------------------------- MODULE RolloutDispatch ----------------------------
EXTENDS Naturals
CONSTANT Fault
VARIABLES phase, sends, pending, recorded, alive, cleanup, released, healthy, sentReady
vars == <<phase,sends,pending,recorded,alive,cleanup,released,healthy,sentReady>>
Init == /\ phase = "ADMITTED" /\ sends = 0 /\ pending = FALSE
        /\ recorded = FALSE /\ alive = FALSE /\ cleanup = FALSE /\ released = FALSE
        /\ healthy = TRUE /\ sentReady = TRUE
Dispatch == /\ (phase = "ADMITTED" \/ (Fault = "replay" /\ sends < 2))
            /\ (healthy \/ Fault = "unready_dispatch")
            /\ phase' = "DISPATCHED" /\ sends' = sends + 1 /\ pending' = TRUE
            /\ sentReady' = (sentReady /\ healthy)
            /\ UNCHANGED <<recorded,alive,cleanup,released,healthy>>
\* Explicit recover_create uses this same ADMITTED gate; it does not introduce
\* an independent send transition for DISPATCHED plus a missing worker record.
RecoverCreate == Dispatch
WorkerReadiness == /\ healthy' = ~healthy
                   /\ UNCHANGED <<phase,sends,pending,recorded,alive,cleanup,released,sentReady>>
ReachWorker == /\ pending /\ ~recorded /\ ~released
               /\ recorded' = TRUE /\ alive' = TRUE /\ pending' = FALSE
               /\ UNCHANGED <<phase,sends,cleanup,released,healthy,sentReady>>
Bind == /\ phase = "DISPATCHED" /\ recorded /\ alive
        /\ phase' = "BOUND" /\ UNCHANGED <<sends,pending,recorded,alive,cleanup,released,healthy,sentReady>>
Stop == /\ phase = "BOUND"
        /\ phase' = "STOPPED" /\ alive' = FALSE
        /\ UNCHANGED <<sends,pending,recorded,cleanup,released,healthy,sentReady>>
ProveCleanup == /\ phase = "STOPPED" /\ ~alive /\ ~cleanup
                /\ cleanup' = TRUE /\ UNCHANGED <<phase,sends,pending,recorded,alive,released,healthy,sentReady>>
Release == /\ phase = "STOPPED" /\ (cleanup \/ Fault = "unproved_release")
           /\ phase' = "RELEASED" /\ released' = TRUE
           /\ UNCHANGED <<sends,pending,recorded,alive,cleanup,healthy,sentReady>>
RejectBeforeDispatch == /\ phase = "ADMITTED"
                        /\ phase' = "RELEASED" /\ released' = TRUE /\ cleanup' = TRUE
                        /\ UNCHANGED <<sends,pending,recorded,alive,healthy,sentReady>>
Next == Dispatch \/ RecoverCreate \/ WorkerReadiness \/ ReachWorker \/ Bind \/ Stop \/ ProveCleanup \/ Release \/ RejectBeforeDispatch
NoCreateReplay == sends <= 1
ReleasedHasCleanupProof == released => (cleanup /\ ~alive)
ReleasedIsFinal == released => phase = "RELEASED"
DispatchRequiresAcceptedHealth == sentReady
Spec == Init /\ [][Next]_vars
=============================================================================
