#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define SHARE_LEN 512u
#define HEADER_LEN 32u
#define FILE_LEN (HEADER_LEN + SHARE_LEN)
#define RECORD_HEADER_LEN 32u
#define MAX_PASSWORD 256u

static const uint8_t file_magic[8] = {'S','E','D','S','H','R','1','\0'};
static const uint8_t record_magic[8] = {'S','E','D','P','W','D','1','\0'};

static void secure_zero(void *p, size_t n) {
    volatile uint8_t *v = (volatile uint8_t *)p;
    while (n--) *v++ = 0;
}

static void harden_process(void) {
    struct rlimit rl;
    rl.rlim_cur = 0;
    rl.rlim_max = 0;
    (void)setrlimit(RLIMIT_CORE, &rl);
}

static int read_exact_regular(const char *path, uint8_t *buf, size_t len) {
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;

    struct stat st;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || (size_t)st.st_size != len) {
        close(fd);
        errno = EINVAL;
        return -1;
    }

    size_t off = 0;
    while (off < len) {
        ssize_t n = read(fd, buf + off, len - off);
        if (n <= 0) {
            close(fd);
            return -1;
        }
        off += (size_t)n;
    }

    uint8_t extra;
    if (read(fd, &extra, 1) != 0) {
        close(fd);
        errno = EINVAL;
        return -1;
    }

    close(fd);
    return 0;
}

static uint16_t get_le16(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static int write_all(int fd, const uint8_t *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t n = write(fd, buf + off, len - off);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        off += (size_t)n;
    }
    return 0;
}

static int reconstruct_password(const char *machine_path,
                                const char *token_path,
                                uint8_t *password,
                                size_t *password_len) {
    uint8_t machine[FILE_LEN];
    uint8_t token[FILE_LEN];
    uint8_t record[SHARE_LEN];
    int rc = -1;

    memset(machine, 0, sizeof(machine));
    memset(token, 0, sizeof(token));
    memset(record, 0, sizeof(record));
    memset(password, 0, MAX_PASSWORD);
    *password_len = 0;

    (void)mlock(machine, sizeof(machine));
    (void)mlock(token, sizeof(token));
    (void)mlock(record, sizeof(record));
    (void)mlock(password, MAX_PASSWORD);

    if (read_exact_regular(machine_path, machine, sizeof(machine)) != 0 ||
        read_exact_regular(token_path, token, sizeof(token)) != 0) {
        goto out;
    }

    if (memcmp(machine, file_magic, sizeof(file_magic)) != 0 ||
        memcmp(token, file_magic, sizeof(file_magic)) != 0 ||
        machine[8] != 1 || token[8] != 1 ||
        memcmp(machine + 16, token + 16, 16) != 0) {
        goto out;
    }

    for (size_t i = 0; i < SHARE_LEN; ++i) {
        record[i] = machine[HEADER_LEN + i] ^ token[HEADER_LEN + i];
    }

    if (memcmp(record, record_magic, sizeof(record_magic)) != 0 || record[8] != 1) {
        goto out;
    }

    uint16_t pwlen = get_le16(record + 10);
    if (pwlen == 0 || pwlen > MAX_PASSWORD || RECORD_HEADER_LEN + pwlen > SHARE_LEN) {
        goto out;
    }

    for (uint16_t i = 0; i < pwlen; ++i) {
        uint8_t b = record[RECORD_HEADER_LEN + i];
        if (b < 0x20 || b > 0x7e) {
            goto out;
        }
        password[i] = b;
    }

    *password_len = pwlen;
    rc = 0;

out:
    secure_zero(record, sizeof(record));
    secure_zero(machine, sizeof(machine));
    secure_zero(token, sizeof(token));
    (void)munlock(record, sizeof(record));
    (void)munlock(machine, sizeof(machine));
    (void)munlock(token, sizeof(token));
    return rc;
}

static int run_linuxpba_with_password(const char *linuxpba_path,
                                      uint8_t *password,
                                      size_t password_len) {
    int pipefd[2];
    if (pipe(pipefd) != 0) return 1;

    pid_t pid = fork();
    if (pid < 0) {
        close(pipefd[0]);
        close(pipefd[1]);
        return 1;
    }

    if (pid == 0) {
        close(pipefd[1]);
        if (dup2(pipefd[0], STDIN_FILENO) < 0) _exit(126);
        close(pipefd[0]);

        int errfd = open("/tmp/pbaerror.log", O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        if (errfd >= 0) {
            (void)dup2(errfd, STDERR_FILENO);
            close(errfd);
        }

        char *const child_argv[] = {(char *)linuxpba_path, NULL};
        execv(linuxpba_path, child_argv);
        _exit(127);
    }

    close(pipefd[0]);
    int write_rc = 0;
    const uint8_t nl = '\n';
    if (write_all(pipefd[1], password, password_len) != 0 ||
        write_all(pipefd[1], &nl, 1) != 0) {
        write_rc = 1;
    }
    close(pipefd[1]);

    secure_zero(password, MAX_PASSWORD);

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno == EINTR) continue;
        return 1;
    }

    if (write_rc != 0) return 1;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}

static void usage(void) {
    fprintf(stderr,
            "usage:\n"
            "  sedtoken --run-linuxpba MACHINE_SHARE USB_SHARE /sbin/linuxpba\n");
}

int main(int argc, char **argv) {
    harden_process();
    signal(SIGPIPE, SIG_IGN);

    if (argc != 5) {
        usage();
        return 64;
    }

    uint8_t password[MAX_PASSWORD];
    size_t password_len = 0;
    memset(password, 0, sizeof(password));
    (void)mlock(password, sizeof(password));

    if (reconstruct_password(argv[2], argv[3], password, &password_len) != 0) {
        secure_zero(password, sizeof(password));
        return 1;
    }

    int rc;
    if (strcmp(argv[1], "--run-linuxpba") == 0) {
        rc = run_linuxpba_with_password(argv[4], password, password_len);
    } else {
        usage();
        secure_zero(password, sizeof(password));
        rc = 64;
    }

    (void)munlock(password, sizeof(password));
    return rc;
}
