------------------------- MODULE QemuNetworkLease -------------------------
EXTENDS TLC, Naturals
CONSTANT Fault
VARIABLE s
vars == <<s>>
Init == s = [phase |-> "ALLOCATING", policy |-> FALSE, up |-> FALSE,
             sent |-> 0, child |-> "ABSENT", committed |-> FALSE,
             retired |-> FALSE]
Install == /\ s.phase = "ALLOCATING" /\ ~s.policy
           /\ s' = [s EXCEPT !.policy = TRUE]
Activate == /\ s.phase = "ALLOCATING"
            /\ (s.policy \/ Fault = "early_up")
            /\ s' = [s EXCEPT !.up = TRUE, !.phase = "READY"]
Lend == /\ (s.phase = "READY" \/ (Fault = "repeat_lend" /\ s.phase = "LENT"))
        /\ s.sent < 2
        /\ s' = [s EXCEPT !.sent = @+1, !.phase = "LENT"]
Commit == /\ s.phase = "LENT" /\ s.child = "ABSENT"
          /\ s' = [s EXCEPT !.child = "WAIT", !.committed = TRUE]
Claim == /\ s.child = "WAIT" /\ s.committed
         /\ (s.phase = "LENT" \/ (Fault = "late_claim" /\ s.retired))
         /\ s' = [s EXCEPT !.phase = "CLAIMED"]
Run == /\ s.phase = "CLAIMED" /\ s.child = "WAIT" /\ s.up /\ s.policy
       /\ s' = [s EXCEPT !.child = "RUN"]
Death == /\ s.child \in {"WAIT", "RUN"}
         /\ s' = [s EXCEPT !.child = "DEAD"]
Retire == /\ s.phase \in {"ALLOCATING", "READY", "LENT", "CLAIMED"}
          /\ (s.phase # "CLAIMED" \/ s.child = "DEAD" \/ Fault = "live_teardown")
          /\ s' = [s EXCEPT !.phase = "RETIRING", !.retired = TRUE]
Remove == /\ s.phase = "RETIRING"
          /\ s' = [s EXCEPT !.phase = "RETIRED", !.up = FALSE, !.policy = FALSE]
Next == Install \/ Activate \/ Lend \/ Commit \/ Claim \/ Run \/ Death \/ Retire \/ Remove
ActiveHasPolicy == s.up => s.policy
OneShotDescriptor == s.sent <= 1
RetiredNeverReopens == s.retired => s.phase \in {"RETIRING", "RETIRED"}
LiveNeedsNetwork == s.child = "RUN" => s.up /\ s.policy
Spec == Init /\ [][Next]_vars
=============================================================================
