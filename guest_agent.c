/* Trusted single-client prototype. Request: timeout_ms limit length\n + command.
 * Response: exit_code timed_out truncated length\n + merged stdout/stderr.
 * @state returns an in-memory token and counter for snapshot verification.
 */
#define _GNU_SOURCE
#include <sys/socket.h>
#include <linux/vm_sockets.h>
#include <sys/wait.h>
#include <sys/random.h>
#include <sys/prctl.h>
#include <poll.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

#include "guest_native.c"

#ifndef DSEC_MAX_TIMEOUT_MS
#define DSEC_MAX_TIMEOUT_MS 30000
#endif

static long milliseconds(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
static int exact(int fd, char *p, size_t n, int writing) {
    while (n) {
        ssize_t k = writing ? write(fd, p, n) : read(fd, p, n);
        if (k < 0 && errno == EINTR) continue;
        if (k <= 0) return -1;
        p += k; n -= k;
    }
    return 0;
}
static void handle(int client, unsigned long long token, unsigned long *counter) {
    char header[128], command[65537]; size_t h = 0;
    while (h < sizeof(header)-1) {
        if (exact(client, header+h, 1, 0)) return;
        if (header[h++] == '\n') break;
    }
    header[h] = 0;
    unsigned timeout, limit, length;
    if (sscanf(header, "%u %u %u", &timeout, &limit, &length) != 3 ||
        !timeout || timeout > DSEC_MAX_TIMEOUT_MS || !limit || limit > 1048576 || length > 65536) return;
    if (exact(client, command, length, 0)) return;
    command[length] = 0;
    char *output = malloc(limit); if (!output) return;
    size_t used = 0; int timedout = 0, truncated = 0, code = 0;
    if (!strcmp(command, "@state")) {
        char value[128];
        int size = snprintf(value, sizeof(value), "%llx %lu\n", token, ++*counter);
        used = (unsigned)size > limit ? limit : (unsigned)size;
        truncated = (unsigned)size > limit;
        memcpy(output, value, used);
    } else {
        int p[2]; if (pipe2(p, O_CLOEXEC)) { free(output); return; }
        pid_t pid = fork();
        if (!pid) {
            setpgid(0, 0);
            int nullfd = open("/dev/null", O_RDONLY);
            dup2(nullfd, 0); dup2(p[1], 1); dup2(p[1], 2);
            close(p[0]); close(p[1]); close(client);
            execl("/bin/sh", "sh", "-c", command, (char *)0);
            _exit(127);
        }
        close(p[1]);
        if (pid < 0) { close(p[0]); free(output); return; }
        setpgid(pid, pid);
        fcntl(p[0], F_SETFL, O_NONBLOCK);
        long deadline = milliseconds() + timeout;
        int status = 0, done = 0, eof = 0;
        while (!done || !eof) {
            char buf[4096]; ssize_t n = read(p[0], buf, sizeof(buf));
            if (n > 0) {
                size_t take = (size_t)n < limit-used ? (size_t)n : limit-used;
                memcpy(output+used, buf, take); used += take;
                if (take < (size_t)n) truncated = 1;
            } else if (!n) eof = 1;
            if (!done && waitpid(pid, &status, WNOHANG) == pid) {
                done = 1; kill(-pid, SIGKILL); /* No detached jobs in this prototype. */
            }
            if (!done && milliseconds() >= deadline) {
                timedout = 1; kill(-pid, SIGKILL);
                while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
                done = 1;
            }
            if (done && eof) break;
            struct pollfd pf = {.fd=p[0], .events=POLLIN}; poll(&pf, 1, 5);
        }
        close(p[0]);
        while (waitpid(-1, NULL, WNOHANG) > 0) {}
        code = timedout ? 124 : WIFEXITED(status) ? WEXITSTATUS(status) : 128+WTERMSIG(status);
    }
    int n = snprintf(header, sizeof(header), "%d %d %d %zu\n", code, timedout, truncated, used);
    if (!exact(client, header, n, 1)) exact(client, output, used, 1);
    free(output);
}
int main(void) {
    signal(SIGPIPE, SIG_IGN);
    prctl(PR_SET_CHILD_SUBREAPER, 1);
    unsigned long long token; unsigned long counter = 0;
    if (getrandom(&token, sizeof(token), 0) != sizeof(token)) return 1;
    if (dsec_native_spawn()) { perror("native vsock"); return 1; }
    int server = socket(AF_VSOCK, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_vm addr = {.svm_family=AF_VSOCK, .svm_port=5000, .svm_cid=VMADDR_CID_ANY};
    if (server < 0 || bind(server, (void *)&addr, sizeof(addr)) || listen(server, 8)) { perror("vsock"); return 1; }
    puts("DSEC_AGENT_READY"); fflush(stdout);
    for (;;) {
        int client = accept4(server, NULL, NULL, SOCK_CLOEXEC);
        if (client < 0) continue;
        struct timeval tv = {.tv_sec=35};
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
        handle(client, token, &counter); close(client);
    }
}
