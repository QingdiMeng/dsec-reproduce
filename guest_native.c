/* Compatibility build entry point for the shared native agent.
 * Canonical responsibilities live in native/: proxy, session and file service.
 * One executable forks distinct aether and chronus processes; no extra runtime
 * binaries are needed inside a chroot or a prepared guest image.
 */
#include "native/protocol.h"
#ifndef DSEC_CHRONUS_SOURCE
#define DSEC_CHRONUS_SOURCE "native/chronus.c"
#endif
#include "native/http.c"
#include DSEC_CHRONUS_SOURCE
#include "native/files.c"
#include "native/aether.c"
