---------------------- MODULE QemuCommandRecovery ----------------------
EXTENDS Naturals, TLC
CONSTANT Fault
VARIABLE s
vars == <<s>>
Init == s = [host |-> "NONE", guest |-> "NONE", effects |-> 0,
              live |-> TRUE, exactProof |-> FALSE]
Admit == /\ s.host = "NONE" /\ s.live
         /\ s' = [s EXCEPT !.host = "PENDING", !.guest = "RUNNING", !.effects = 1]
GuestDone == /\ s.guest = "RUNNING" /\ s.live
             /\ s' = [s EXCEPT !.guest = "DONE"]
Reply == /\ s.host = "PENDING" /\ s.guest = "DONE"
         /\ s' = [s EXCEPT !.host = "DONE", !.exactProof = TRUE]
LostReplyOrOwner == /\ s.host = "PENDING"
                   /\ s' = [s EXCEPT !.host = "UNKNOWN"]
Recover == /\ s.host = "UNKNOWN" /\ s.guest = "DONE" /\ s.live
           /\ s' = [s EXCEPT !.host = "DONE", !.exactProof = TRUE]
BadProof == /\ Fault = "unmatched" /\ s.host = "UNKNOWN"
            /\ s' = [s EXCEPT !.host = "DONE", !.exactProof = FALSE]
Replay == /\ Fault = "replay" /\ s.host = "UNKNOWN" /\ s.live /\ s.effects = 1
          /\ s' = [s EXCEPT !.host = "PENDING", !.effects = 2, !.guest = "RUNNING"]
Death == /\ s.live /\ s' = [s EXCEPT !.live = FALSE,
             !.guest = IF s.guest = "RUNNING" THEN "UNKNOWN" ELSE @]
Next == Admit \/ GuestDone \/ Reply \/ LostReplyOrOwner \/ Recover \/ BadProof \/ Replay \/ Death \/ UNCHANGED vars
AtMostOnce == s.effects <= 1
DoneRequiresExactProof == s.host = "DONE" => s.guest = "DONE" /\ s.exactProof
InspectionDoesNotQueue == ~(Fault = "blocking_inspection" /\ s.guest = "RUNNING" /\ s.live)
Spec == Init /\ [][Next]_vars
=============================================================================
