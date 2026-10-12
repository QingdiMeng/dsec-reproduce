--------------------------- MODULE QemuLifecycle ----------------------------
EXTENDS Naturals
CONSTANT Fault
VARIABLES state, busy, process, written, boots, closed
vars == <<state, busy, process, written, boots, closed>>
Init == /\ state = "NEW" /\ busy = "none" /\ process = FALSE
        /\ written = FALSE /\ boots = 0 /\ closed = FALSE
Start == /\ busy = "none"
         /\ ((state = "NEW" /\ ~written) \/ (Fault = "replay" /\ state = "STOPPED" /\ boots < 2))
         /\ state' = "STARTING" /\ busy' = "boot" /\ written' = TRUE
         /\ boots' = boots + 1 /\ UNCHANGED <<process, closed>>
Spawn == /\ state = "STARTING" /\ busy = "boot" /\ ~process
         /\ process' = TRUE /\ UNCHANGED <<state, busy, written, boots, closed>>
Ready == /\ busy = "boot" /\ process
         /\ (state = "STARTING" \/ (Fault = "late_ready" /\ state = "STOPPED"))
         /\ state' = "RUNNING" /\ busy' = "none"
         /\ UNCHANGED <<process, written, boots, closed>>
Pause == /\ state = "RUNNING" /\ busy = "none"
         /\ state' = "PAUSING" /\ busy' = "control"
         /\ UNCHANGED <<process, written, boots, closed>>
Resume == /\ state = "PAUSED" /\ busy = "none"
          /\ state' = "RESUMING" /\ busy' = "control"
          /\ UNCHANGED <<process, written, boots, closed>>
ControlDone == /\ busy = "control"
               /\ state \in {"PAUSING", "RESUMING"}
               /\ state' = IF state = "PAUSING" THEN "PAUSED" ELSE "RUNNING"
               /\ busy' = "none" /\ UNCHANGED <<process, written, boots, closed>>
Stop == /\ (busy = "none" \/ (Fault = "late_ready" /\ busy = "boot"))
        /\ state # "STOPPED" /\ state' = "STOPPED" /\ closed' = TRUE
        /\ busy' = IF Fault = "late_ready" /\ busy = "boot" THEN busy ELSE "none"
        /\ process' = IF Fault = "late_ready" /\ busy = "boot" THEN process ELSE FALSE
        /\ written' = TRUE /\ UNCHANGED boots
RecoverPending == /\ busy # "none"
                  /\ state' = "FAILED" /\ busy' = "none" /\ process' = FALSE
                  /\ UNCHANGED <<written, boots, closed>>
Next == Start \/ Spawn \/ Ready \/ Pause \/ Resume \/ ControlDone \/ Stop \/ RecoverPending
StoppedIsFinal == closed => state = "STOPPED"
NoBootReplay == boots <= 1
Spec == Init /\ [][Next]_vars
=============================================================================
