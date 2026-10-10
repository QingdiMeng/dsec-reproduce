/* Force-included ONLY in developer builds of the real guest_native.c.
 * Fixed-size writes are async-signal-safe; no stdio or allocation in handler.
 * Phases: 0=RUN, 1=ASSIGN, 2=IDLE. A trace is an observed code-window prefix. */
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <string.h>
static int ns_probe_fd=-1;
static volatile sig_atomic_t ns_probe_phase, ns_probe_returned, ns_probe_signalled;
static const char *ns_probe_timing;
static void ns_probe_emit(int stopping) {
    int saved=errno;
    int32_t record[5]={(int32_t)getpid(),stopping,ns_probe_signalled,
                       ns_probe_phase,ns_probe_returned};
    ssize_t n;
    do {n=write(ns_probe_fd,record,sizeof(record));} while(n<0 && errno==EINTR);
    if(n!=(ssize_t)sizeof(record))_exit(97);
    errno=saved;
}
static void ns_probe_begin(int stopping) {
    const char *fd=getenv("DSEC_TEST_TRACE_FD");
    if(!fd || ns_probe_fd>=0)_exit(98); /* One command per controlled instance. */
    ns_probe_fd=atoi(fd);ns_probe_timing=getenv("DSEC_TEST_SIGNAL_TIMING");
    if(!ns_probe_timing)_exit(98);
    ns_probe_phase=0;ns_probe_returned=0;ns_probe_signalled=0;
    ns_probe_emit(stopping);
    if(!strcmp(ns_probe_timing,"before_return"))raise(SIGTERM);
}
static void ns_probe_signal(int stopping) {
    if(ns_probe_fd<0)return; /* Main service is outside the session window. */
    ns_probe_signalled=1;ns_probe_emit(stopping);
}
static void ns_probe_return(int stopping,int result) {
    ns_probe_returned=result;ns_probe_phase=1;ns_probe_emit(stopping);
    if(!strcmp(ns_probe_timing,"between_return_and_apply"))raise(SIGTERM);
}
static void ns_probe_apply(int stopping) {
    ns_probe_phase=2;ns_probe_emit(stopping);
    if(!strcmp(ns_probe_timing,"after_apply"))raise(SIGTERM);
}
#define NS_TEST_BEFORE_RUN() ns_probe_begin(ns_stopping)
#define NS_TEST_AFTER_RUN(result) ns_probe_return(ns_stopping,result)
#define NS_TEST_AFTER_APPLY() ns_probe_apply(ns_stopping)
#define NS_TEST_SIGNAL() ns_probe_signal(ns_stopping)
