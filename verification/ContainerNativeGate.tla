------------------------ MODULE ContainerNativeGate ------------------------
EXTENDS TLC
CONSTANT Fault
VARIABLE s
vars == <<s>>
Init == s = [active |-> {}, stopping |-> FALSE, stopped |-> FALSE, rejected |-> FALSE]
Admit(i) == /\ i \notin s.active /\ ~s.stopping /\ ~s.stopped
            /\ s' = [s EXCEPT !.active = @ \cup {i}]
Release(i) == /\ i \in s.active /\ s' = [s EXCEPT !.active = @ \ {i}]
StopPermit == /\ ~s.stopping /\ ~s.stopped
              /\ (s.active = {} \/ Fault = "missing_guard")
              /\ s' = [s EXCEPT !.stopping = TRUE]
StopCommit == /\ s.stopping /\ s' = [s EXCEPT !.stopping = FALSE, !.stopped = TRUE]
Reject == /\ (s.active # {} \/ s.stopping \/ s.stopped) /\ ~s.rejected
          /\ s' = [s EXCEPT !.rejected = TRUE]
Next == (\E i \in {1, 2}: Admit(i) \/ Release(i)) \/ StopPermit \/ StopCommit \/ Reject
StopExcludesNativeActivity == s.stopping \/ s.stopped => s.active = {}
Spec == Init /\ [][Next]_vars
=============================================================================
