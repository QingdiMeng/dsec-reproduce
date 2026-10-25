--------------------------- MODULE FnCallSlot ---------------------------
EXTENDS TLC, Naturals
CONSTANT Fault
VARIABLE s
vars == <<s>>
Calls == {1, 2}
Init == s = [phase |-> "READY", dirty |-> FALSE, live |-> TRUE,
              lease |-> TRUE, current |-> 0, started |-> {}, done |-> {},
              unknown |-> {}, replay |-> FALSE]
Admit(i) == /\ s.phase = "READY" /\ s.live /\ s.lease
            /\ i \notin s.done /\ (i \notin s.unknown \/ Fault = "replay_unknown")
            /\ s' = [s EXCEPT !.phase = "RUNNING", !.dirty = TRUE,
                      !.current = i, !.started = @ \cup {i},
                      !.replay = @ \/ (i \in s.unknown)]
Finish == /\ s.phase = "RUNNING"
          /\ s' = [s EXCEPT !.phase = "CLEANING"]
Clean == /\ s.phase = "CLEANING"
         /\ s' = [s EXCEPT !.phase = "READY", !.dirty = FALSE,
                   !.done = @ \cup {s.current}, !.current = 0]
EarlyReady == /\ Fault = "early_ready" /\ s.phase = "CLEANING"
              /\ s' = [s EXCEPT !.phase = "READY"]
Lose == /\ s.phase \in {"RUNNING", "CLEANING"}
        /\ s' = [s EXCEPT !.phase = IF Fault = "replay_unknown" THEN "READY" ELSE "RETIRING",
                  !.unknown = @ \cup {s.current}]
Stop == /\ s.phase \in {"READY", "RUNNING", "CLEANING"}
        /\ s' = [s EXCEPT !.phase = "RETIRING"]
Death == /\ s.phase = "RETIRING" /\ s.live
         /\ s' = [s EXCEPT !.live = FALSE]
Release == /\ s.phase = "RETIRING" /\ (~s.live \/ Fault = "release_live")
           /\ s' = [s EXCEPT !.lease = FALSE, !.phase = "STOPPED"]
Next == (\E i \in Calls: Admit(i)) \/ Finish \/ Clean \/ EarlyReady \/ Lose \/ Stop \/ Death \/ Release
ReadyIsClean == s.phase = "READY" => ~s.dirty
UnknownNeverReplayed == ~s.replay
LiveHasLease == s.live => s.lease
StoppedIsTerminal == s.phase = "STOPPED" => ~s.live
Spec == Init /\ [][Next]_vars
=============================================================================
