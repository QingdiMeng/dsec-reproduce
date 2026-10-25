--------------------------- MODULE QemuHostGate ---------------------------
EXTENDS TLC
CONSTANT Fault
VARIABLE s
vars == <<s>>
Init == s = [phase |-> "PREPARED", loaded |-> TRUE, launcher |-> "WAIT",
              committed |-> FALSE, grouped |-> FALSE, confined |-> FALSE,
              removing |-> FALSE]
Commit == /\ s.launcher = "WAIT" /\ ~s.committed
          /\ s' = [s EXCEPT !.committed = TRUE]
Claim == /\ s.launcher = "WAIT" /\ s.committed
         /\ (s.phase = "PREPARED" \/ (Fault = "stale_claim" /\ s.phase = "RETIRED"))
         /\ s.loaded /\ s' = [s EXCEPT !.phase = "CLAIMED"]
Go == /\ s.launcher = "WAIT"
      /\ (s.phase = "CLAIMED" \/ Fault = "uncommitted")
      /\ s' = [s EXCEPT !.launcher = "GO"]
Group == /\ s.launcher = "GO" /\ ~s.grouped
         /\ s' = [s EXCEPT !.grouped = TRUE]
Exec == /\ s.launcher = "GO" /\ s.loaded
        /\ (s.grouped \/ Fault = "outside_group")
        /\ s' = [s EXCEPT !.launcher = "EXEC", !.confined = TRUE]
Death == /\ s.launcher \in {"WAIT", "GO", "EXEC"}
         /\ s' = [s EXCEPT !.launcher = "DEAD"]
Retire == /\ s.phase \in {"PREPARED", "CLAIMED"}
          /\ (s.phase # "CLAIMED" \/ s.launcher = "DEAD" \/ Fault = "early_unload")
          /\ s' = [s EXCEPT !.phase = "RETIRED", !.removing = TRUE]
Unload == /\ s.removing /\ s.loaded
          /\ s' = [s EXCEPT !.loaded = FALSE, !.removing = FALSE]
Next == Commit \/ Claim \/ Go \/ Group \/ Exec \/ Death \/ Retire \/ Unload
RunningIsConfined == s.launcher = "EXEC" => s.grouped /\ s.confined /\ s.loaded
ExecHasCommittedIntent == s.launcher = "EXEC" => s.committed
Spec == Init /\ [][Next]_vars
=============================================================================
