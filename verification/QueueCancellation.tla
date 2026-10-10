------------------------ MODULE QueueCancellation ------------------------
EXTENDS TLC
CONSTANT Fault
VARIABLE s
vars == <<s>>
Init == s = [phase |-> "QUEUED", intent |-> FALSE, reply |-> "NONE",
             queuedCancel |-> FALSE, executed |-> FALSE, result |-> "PENDING"]
Dispatch == /\ s.phase = "QUEUED" /\ (~s.intent \/ Fault = "redispatch")
            /\ s' = [s EXCEPT !.phase = "DISPATCHING"]
Busy == /\ s.phase = "DISPATCHING" /\ ~s.executed
        /\ s' = [s EXCEPT !.phase = "QUEUED"]
Cancel == /\ s.result = "PENDING" /\ ~s.intent
          /\ IF Fault = "lost_cancel" /\ s.phase = "QUEUED"
             THEN s' = [s EXCEPT !.reply = "FALSE"]
             ELSE s' = [s EXCEPT !.intent = TRUE,
                         !.queuedCancel = (s.phase = "QUEUED")]
Reply == /\ s.intent /\ s.reply = "NONE"
         /\ s' = [s EXCEPT !.reply = "TRUE"]
Start == /\ s.phase = "DISPATCHING" /\ ~s.executed
         /\ s' = [s EXCEPT !.executed = TRUE]
Finish == /\ s.result = "PENDING"
          /\ IF s.phase = "QUEUED" /\ s.intent
             THEN s' = [s EXCEPT !.phase = "DONE", !.result = "CANCELLED"]
             ELSE /\ s.phase = "DISPATCHING" /\ s.executed
                  /\ s' \in {[s EXCEPT !.phase = "DONE", !.result = r] :
                               r \in {"SUCCESS", "CANCELLED", "UNKNOWN"}}
LateCancel == /\ s.result # "PENDING" /\ s.reply # "FALSE"
              /\ s' = [s EXCEPT !.reply = "FALSE"]
Next == Dispatch \/ Busy \/ Cancel \/ Reply \/ Start \/ Finish \/ LateCancel
QueuedCancellationPreventsStart == s.queuedCancel => ~s.executed
PendingCancelIsAcknowledged == s.reply = "FALSE" => s.result # "PENDING"
Spec == Init /\ [][Next]_vars
=============================================================================
