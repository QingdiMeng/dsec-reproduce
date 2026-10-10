------------------------- MODULE ShutdownSignal -------------------------
EXTENDS TLC
CONSTANT UnsafeAssignment
VARIABLES stop, signalled, phase, returned
vars == <<stop, signalled, phase, returned>>
Init == /\ stop = FALSE /\ signalled = FALSE
        /\ phase = "RUN" /\ returned = FALSE
Signal == /\ ~signalled /\ stop' = TRUE /\ signalled' = TRUE
          /\ UNCHANGED <<phase, returned>>
\* ns_run may also request shell reset on timeout/exit, independently of SIGTERM.
Return == /\ phase = "RUN" /\ returned' \in BOOLEAN /\ phase' = "ASSIGN"
          /\ UNCHANGED <<stop, signalled>>
\* Safe concrete code: if (returned) stop = 1; otherwise no write to stop.
\* Do NOT implement it as stop |= returned: that read-modify-write can race
\* with a signal too. Assign abstracts one write, or an actual no-write branch.
Assign == /\ phase = "ASSIGN" /\ phase' = "IDLE"
          /\ (IF UnsafeAssignment THEN stop' = returned
              ELSE IF returned THEN stop' = TRUE ELSE UNCHANGED stop)
          /\ UNCHANGED <<signalled, returned>>
Exit == /\ phase = "IDLE" /\ stop /\ phase' = "EXIT"
        /\ UNCHANGED <<stop, signalled, returned>>
Next == Signal \/ Return \/ Assign \/ Exit
State == [stop |-> stop, signalled |-> signalled, phase |-> phase, returned |-> returned]
StopIsMonotonic == signalled => stop
Spec == Init /\ [][Next]_vars
=============================================================================
