------------------------ MODULE NativeLifecycle ------------------------
EXTENDS Integers, FiniteSets, TLC

CONSTANTS Requests, MaxEpoch, Fault
ASSUME /\ Requests # {} /\ MaxEpoch \in Nat
       /\ Fault \in {"none", "queued_cancel", "stale_failure", "unknown_replay",
                     "unfenced_start"}

VARIABLE s
vars == <<s>>
Key(q) == <<s.epoch, q>>
Pending(q) == s.req[q] \in {"ADMITTED", "CANCELLING"}
Busy == {q \in Requests : Pending(q) /\ s.owner[q] = s.epoch}
States == {"ABSENT", "QUEUED", "ADMITTED", "CANCELLING", "DONE",
           "CANCELLED", "UNKNOWN"}

Init == s = [life |-> "RUNNING", epoch |-> 0, edge |-> TRUE, vm |-> TRUE,
    req |-> [q \in Requests |-> "ABSENT"], owner |-> [q \in Requests |-> 0],
    executions |-> [q \in Requests |-> 0], settled |-> [q \in Requests |-> -1],
    starts |-> {}, results |-> {}, failures |-> {}, cancels |-> {},
    seen |-> {}, blocked |-> {}, active |-> {}, stopped |-> {},
    replayed |-> FALSE, staleStart |-> FALSE, staleCallback |-> FALSE]

Submit(q) ==
    /\ s.edge /\ s.life = "RUNNING" /\ s.req[q] = "ABSENT"
    /\ s' = [s EXCEPT !.req[q] = "QUEUED", !.owner[q] = s.epoch]

\* Admission is a durable Edge decision, BEFORE sending an execution permit.
Admit(q) ==
    /\ s.edge /\ s.life = "RUNNING"
    /\ (s.req[q] = "QUEUED" \/
        (Fault = "queued_cancel" /\ s.req[q] = "CANCELLED") \/
        (Fault = "unknown_replay" /\ s.req[q] = "UNKNOWN"))
    /\ s' = [s EXCEPT !.req[q] = "ADMITTED", !.owner[q] = s.epoch,
         !.starts = @ \cup {Key(q)},
         !.replayed = @ \/ (s.req[q] = "UNKNOWN")]

\* Guest actions do NOT read Edge's request state. A host transport delivers
\* each admitted start at most once: API retries attach to Edge's journal;
\* UNKNOWN is never retransmitted. Replies can be delayed and duplicated.
\* seen is a history monitor, NOT a second guest execution journal.
GuestStart(e, q) ==
    /\ <<e, q>> \in s.starts
    /\ <<e, q>> \notin s.blocked
    /\ ((s.vm /\ s.life # "PAUSED" /\ e = s.epoch) \/
        (Fault = "unfenced_start" /\ s.life = "STOPPED"))
    /\ s' = [s EXCEPT !.starts = @ \ {<<e, q>>}, !.seen = @ \cup {<<e, q>>},
         !.active = @ \cup {q}, !.executions[q] = @ + 1,
         !.staleStart = @ \/ (~s.vm \/ e # s.epoch)]

GuestFinish(q) ==
    /\ s.vm /\ q \in s.active
    /\ s' = [s EXCEPT !.active = @ \ {q}, !.results = @ \cup {Key(q)}]

CommitResult(e, q) ==
    /\ s.edge /\ e = s.epoch /\ e = s.owner[q]
    /\ s.life \in {"RUNNING", "STOPPING"}
    /\ <<e, q>> \in s.results /\ Pending(q)
    /\ s' = [s EXCEPT !.req[q] = "DONE", !.settled[q] = s.executions[q]]

CancelQueued(q) ==
    /\ s.edge /\ s.req[q] = "QUEUED"
    /\ s' = [s EXCEPT !.req[q] = "CANCELLED", !.settled[q] = s.executions[q]]

RequestCancel(q) ==
    /\ s.edge /\ s.req[q] = "ADMITTED" /\ s.owner[q] = s.epoch
    /\ s' = [s EXCEPT !.req[q] = "CANCELLING", !.cancels = @ \cup {Key(q)}]

GuestCancel(e, q) ==
    /\ s.vm /\ e = s.epoch /\ <<e, q>> \in s.cancels
    /\ <<e, q>> \notin s.blocked
    /\ s' = [s EXCEPT !.blocked = @ \cup {<<e, q>>}, !.active = @ \ {q}]

CommitCancel(q) ==
    /\ s.edge /\ s.req[q] = "CANCELLING" /\ s.owner[q] = s.epoch
    /\ Key(q) \in s.blocked /\ q \notin s.active
    /\ s' = [s EXCEPT !.req[q] = "CANCELLED", !.settled[q] = s.executions[q]]

RequestStop ==
    /\ s.edge /\ s.life \in {"RUNNING", "PAUSED", "FAILED"}
    /\ s' = [s EXCEPT !.life = "STOPPING"]

\* Stop request closes admission; old permits MAY still run before VMM death.
\* STOPPED is committed only after VM death, not at stop-request acceptance.
StopGuest ==
    /\ s.life = "STOPPING" /\ s.vm
    /\ s' = [s EXCEPT !.vm = FALSE, !.active = {},
         !.failures = @ \cup {Key(q) : q \in Busy}]

CommitStop ==
    /\ s.edge /\ s.life = "STOPPING" /\ ~s.vm
    /\ s' = [s EXCEPT !.life = "STOPPED", !.stopped = @ \cup {s.epoch},
         !.req = [q \in Requests |-> IF Pending(q) THEN "UNKNOWN" ELSE s.req[q]]]

LateFailure(e, q) ==
    /\ s.edge /\ <<e, q>> \in s.failures
    /\ ((e = s.epoch /\ e = s.owner[q] /\ Pending(q) /\
          s.life = "RUNNING") \/
        (Fault = "stale_failure" /\ s.life \in {"STOPPED", "RUNNING"}))
    /\ s' = [s EXCEPT !.life = "FAILED", !.req[q] = "UNKNOWN",
         !.staleCallback = @ \/ (e # s.epoch)]

CrashEdge == /\ s.edge /\ s' = [s EXCEPT !.edge = FALSE]
RestartEdge ==
    /\ ~s.edge
    /\ s' = [s EXCEPT !.edge = TRUE,
         !.life = IF Busy # {} /\ s.life # "STOPPED" THEN "STOPPING" ELSE @,
         !.req = [q \in Requests |-> IF Pending(q) THEN "UNKNOWN" ELSE s.req[q]]]

Pause ==
    /\ s.edge /\ s.life = "RUNNING" /\ Busy = {} /\ s.active = {}
    /\ s' = [s EXCEPT !.life = "PAUSED", !.vm = FALSE]
Resume ==
    /\ s.edge /\ s.life = "PAUSED"
    /\ s' = [s EXCEPT !.life = "RUNNING", !.vm = TRUE]

\* Explicit recovery changes incarnation; it never replays UNKNOWN commands.
Recover ==
    /\ s.edge /\ s.life = "STOPPED" /\ ~s.vm /\ s.epoch < MaxEpoch
    /\ s' = [s EXCEPT !.life = "RUNNING", !.vm = TRUE, !.epoch = @ + 1,
         !.owner = [q \in Requests |-> IF s.req[q] = "QUEUED"
                                            THEN s.epoch + 1 ELSE s.owner[q]]]

Next == (\E q \in Requests : Submit(q) \/ Admit(q) \/ GuestFinish(q) \/
            CancelQueued(q) \/ RequestCancel(q) \/ CommitCancel(q))
     \/ (\E e \in 0..MaxEpoch, q \in Requests : GuestStart(e,q) \/
            CommitResult(e,q) \/ GuestCancel(e,q) \/ LateFailure(e,q))
     \/ RequestStop \/ StopGuest \/ CommitStop \/ CrashEdge \/ RestartEdge
     \/ Pause \/ Resume \/ Recover

TypeOK == /\ s.life \in {"RUNNING", "PAUSED", "STOPPING", "STOPPED", "FAILED"}
          /\ s.epoch \in 0..MaxEpoch /\ s.edge \in BOOLEAN /\ s.vm \in BOOLEAN
          /\ s.req \in [Requests -> States]
          /\ s.owner \in [Requests -> 0..MaxEpoch]
          /\ s.executions \in [Requests -> 0..2]
          /\ s.settled \in [Requests -> {-1, 0, 1, 2}]
          /\ s.active \subseteq Requests
          /\ \A field \in {s.starts, s.results, s.failures, s.cancels,
                            s.seen, s.blocked} : field \subseteq (0..MaxEpoch) \X Requests
          /\ s.stopped \subseteq 0..MaxEpoch
          /\ s.replayed \in BOOLEAN /\ s.staleStart \in BOOLEAN
          /\ s.staleCallback \in BOOLEAN
AtMostOnce == \A q \in Requests : s.executions[q] <= 1
TerminalNoNewExecution == \A q \in Requests :
    s.settled[q] >= 0 => s.executions[q] = s.settled[q]
StoppedIsFinal == s.epoch \in s.stopped => s.life = "STOPPED"
StoppedIsQuiescent == s.life = "STOPPED" => (~s.vm /\ s.active = {})
PausedIsQuiescent == s.life = "PAUSED" => (~s.vm /\ Busy = {} /\ s.active = {})
UnknownNeverReadmitted == ~s.replayed
StartIsFenced == ~s.staleStart
CallbackIsFenced == ~s.staleCallback
Spec == Init /\ [][Next]_vars
=============================================================================
