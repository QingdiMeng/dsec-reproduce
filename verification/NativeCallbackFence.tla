------------------------ MODULE NativeCallbackFence ------------------------
EXTENDS TLC
CONSTANT Fault
VARIABLE s
vars == <<s>>
Init == s = [life |-> "RUNNING", epoch |-> 0, opEpoch |-> 0, active |-> FALSE,
             stopped |-> FALSE, unknown |-> FALSE, obsolete |-> FALSE, staleRetired |-> FALSE]
Admit == /\ s.life = "RUNNING" /\ ~s.active /\ ~s.unknown
         /\ s' = [s EXCEPT !.active = TRUE, !.opEpoch = s.epoch]
Stop == /\ s.life # "STOPPED"
        /\ s' = [s EXCEPT !.life = "STOPPED", !.stopped = TRUE]
Retire == /\ s.life = "RUNNING" /\ s.active /\ ~s.unknown
          /\ s' = [s EXCEPT !.life = "FAILED"]
Restore == /\ s.life = "FAILED" /\ s.epoch = 0 /\ ~s.stopped
           /\ s' = [s EXCEPT !.life = "RUNNING", !.epoch = 1]
Callback == /\ s.active /\ ~s.unknown
            /\ s' = [s EXCEPT !.unknown = TRUE,
                      !.obsolete = (s.opEpoch # s.epoch),
                      !.staleRetired = (Fault = "unfenced" /\ s.opEpoch # s.epoch /\ s.life = "RUNNING"),
                      !.life = IF Fault = "unfenced" \/
                                  (s.opEpoch = s.epoch /\ s.life = "RUNNING")
                               THEN "FAILED" ELSE s.life]
Release == /\ s.active /\ s.unknown /\ s' = [s EXCEPT !.active = FALSE]
Next == Admit \/ Stop \/ Retire \/ Restore \/ Callback \/ Release
StoppedIsFinal == s.stopped => s.life = "STOPPED"
OldCallbackCannotRetireReplacement == ~s.staleRetired
Spec == Init /\ [][Next]_vars
=============================================================================
