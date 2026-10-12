/* Native v1 guest service. This file is also built as a standalone UDS agent.
 * Edge owns the durable intent/result journal; this service never retries work.
 * The legacy port 5000 remains in guest_agent.c. Native microVM port is 5001.
 */
#define _GNU_SOURCE
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/file.h>
#ifdef __linux__
#include <linux/vm_sockets.h>
#include <sys/prctl.h>
#endif
#include <dirent.h>
#include <poll.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

#ifndef DSEC_MAX_TIMEOUT_MS
#define DSEC_MAX_TIMEOUT_MS 30000
#endif
#define NS_CHUNK 65536
#define NS_OUTPUT 1048576
#define NS_FILE (64*1024*1024)
#define NS_SESSIONS 32
#define NS_CLIENTS 64
static char ns_root[100];
static char ns_session_id[33];
static volatile sig_atomic_t ns_stopping;
static pid_t ns_shell;
static int ns_stream_client=-1;
/* Compile-time probes are supplied only by the developer verification build.
 * Production agents contain no trace collector or fault-injection behavior. */
#ifndef NS_TEST_BEFORE_RUN
#define NS_TEST_BEFORE_RUN() ((void)0)
#define NS_TEST_AFTER_RUN(result) ((void)0)
#define NS_TEST_AFTER_APPLY() ((void)0)
#define NS_TEST_SIGNAL() ((void)0)
#endif
static void ns_parent_guard(pid_t parent) {
#ifdef __linux__
    if(prctl(PR_SET_PDEATHSIG,SIGTERM) || getppid()!=parent)_exit(1);
#else
    (void)parent;
#endif
}
static long ns_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec*1000+t.tv_nsec/1000000;
}
static int ns_exact(int fd, void *ptr, size_t n, int writing) {
    char *p=ptr;
    while(n) {
        ssize_t k=writing?write(fd,p,n):read(fd,p,n);
        if(k<0 && errno==EINTR && !ns_stopping) continue;
        if(k<=0) return -1;
        p+=k; n-=(size_t)k;
    }
    return 0;
}
static int ns_line(int fd, char *p, size_t size) {
    size_t n=0;
    while(n+1<size) {
        if(ns_exact(fd,p+n,1,0)) return -1;
        if(p[n++]=='\n') {p[n]=0; return 0;}
    }
    return -1;
}
static void ns_timeout(int fd, unsigned ms) {
    struct timeval tv={.tv_sec=ms/1000,.tv_usec=(ms%1000)*1000};
    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&tv,sizeof(tv));
    setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&tv,sizeof(tv));
}
static int ns_reply(int fd, int error, int code, int timeout, int truncated,
                    int reset, const char *out, size_t no, const char *err, size_t ne) {
    char h[128];
    int cancelled=reset && code==125 && !timeout;
    int n=snprintf(h,sizeof(h),"DSEC1 %d %d %d %d %d %d %zu %zu\n",error,code,
                   timeout,truncated,reset,cancelled,no,ne);
    if(ns_exact(fd,h,(size_t)n,1)) return -1;
    if(no && ns_exact(fd,(void *)out,no,1)) return -1;
    if(ne && ns_exact(fd,(void *)err,ne,1)) return -1;
    return 0;
}
static int ns_id(const char *s) {
    if(strlen(s)!=32) return 0;
    for(int i=0;i<32;i++) if(!((s[i]>='0' && s[i]<='9') || (s[i]>='a' && s[i]<='f'))) return 0;
    return 1;
}
static int ns_unix(const char *path, int bind_socket) {
    struct sockaddr_un a={.sun_family=AF_UNIX};
    if(strlen(path)>=sizeof(a.sun_path)) {errno=ENAMETOOLONG; return -1;}
    strcpy(a.sun_path,path);
    int fd=socket(AF_UNIX,SOCK_STREAM,0);
    if(fd<0) return -1;
    fcntl(fd,F_SETFD,FD_CLOEXEC);
    if((bind_socket?bind(fd,(void *)&a,sizeof(a)):connect(fd,(void *)&a,sizeof(a)))) {
        int e=errno; close(fd); errno=e; return -1;
    }
    if(bind_socket && listen(fd,8)) {close(fd); return -1;}
    return fd;
}
static void ns_session_path(char *path, size_t size, const char *id) {
    snprintf(path,size,"%s/%.32s.sock",ns_root,id);
}
static void ns_retire_session(const char *id) {
    char closed[160];snprintf(closed,sizeof(closed),"%s/%.32s.closed",ns_root,id);
    int fd=open(closed,O_CREAT|O_EXCL|O_WRONLY|O_CLOEXEC,0600);
    if(fd>=0)close(fd);
}
static int ns_accept(int listener) {
    int fd=accept(listener,NULL,NULL);
    if(fd>=0 && fcntl(fd,F_SETFL,fcntl(fd,F_GETFL)&~O_NONBLOCK)<0) {
        int saved=errno;close(fd);errno=saved;return -1;
    }
    return fd;
}
static void ns_signal(int sig) { (void)sig; ns_stopping=1; if(ns_shell>0) kill(-ns_shell,SIGKILL); NS_TEST_SIGNAL(); }
static void ns_child_signal(int sig) { (void)sig; }
#ifdef __linux__
/* Commands cannot leave detached work outside the session's lifetime. */
static void ns_children(pid_t parent) {
    DIR *dir=opendir("/proc"); if(!dir) return;
    struct dirent *item;
    while((item=readdir(dir))) {
        char *end; long pid=strtol(item->d_name,&end,10);
        if(*end || pid<=0) continue;
        char path[80],line[4096]; snprintf(path,sizeof(path),"/proc/%ld/stat",pid);
        FILE *f=fopen(path,"r"); if(!f) continue;
        char *read=fgets(line,sizeof(line),f); fclose(f); if(!read) continue;
        char *tail=strrchr(line,')'); char state; long ppid;
        if(tail && sscanf(tail+1," %c %ld",&state,&ppid)==2 && ppid==parent) {
            ns_children((pid_t)pid); kill((pid_t)pid,SIGKILL);
        }
    }
    closedir(dir);
}
#else
static void ns_children(pid_t parent) { (void)parent; }
#endif

static void ns_file(int c, const char *header);
static void ns_upload_cleanup(const char *owner);
static void ns_role(const char *name) {
#ifdef __linux__
    (void)prctl(PR_SET_NAME,name,0,0,0);
#else
    (void)name;
#endif
}
