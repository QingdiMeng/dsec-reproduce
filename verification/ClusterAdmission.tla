---- MODULE ClusterAdmission ----
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Requests, Nodes, Limit, Fault
VARIABLES used, owner, phase, effects, proof, unknown, healthy
vars == <<used, owner, phase, effects, proof, unknown, healthy>>

Init == /\ used = 0
        /\ owner = [r \in Requests |-> "_"]
        /\ phase = [r \in Requests |-> "NONE"]
        /\ effects = [r \in Requests |-> {}]
        /\ proof = [r \in Requests |-> "NONE"]
        /\ unknown = [r \in Requests |-> FALSE]
        /\ healthy = Nodes

Admit(r,n) == /\ phase[r] = "NONE" /\ n \in healthy
              /\ (used < Limit \/ Fault = "quota_guard")
              /\ used' = used + 1
              /\ owner' = [owner EXCEPT ![r] = n]
              /\ phase' = [phase EXCEPT ![r] = "ADMITTED"]
              /\ UNCHANGED <<effects, proof, unknown, healthy>>
Execute(r) == /\ phase[r] = "ADMITTED" /\ effects[r] = {}
              /\ (proof[r] = "NONE" \/ Fault = "late_rejected_dispatch")
              /\ effects' = [effects EXCEPT ![r] = {owner[r]}]
              /\ UNCHANGED <<used, owner, phase, proof, unknown, healthy>>
Bind(r) == /\ phase[r] = "ADMITTED" /\ effects[r] = {owner[r]}
           /\ phase' = [phase EXCEPT ![r] = "BOUND"]
           /\ UNCHANGED <<used, owner, effects, proof, unknown, healthy>>
LoseResponse(r) == /\ phase[r] = "ADMITTED" /\ effects[r] # {}
                   /\ ~unknown[r]
                   /\ unknown' = [unknown EXCEPT ![r] = TRUE]
                   /\ UNCHANGED <<used, owner, phase, effects, proof, healthy>>
RejectBeforeEffect(r) == /\ phase[r] = "ADMITTED" /\ effects[r] = {}
                         /\ proof[r] = "NONE"
                         /\ proof' = [proof EXCEPT ![r] = "NOT_ADMITTED"]
                         /\ UNCHANGED <<used, owner, phase, effects, unknown, healthy>>
StopProof(r) == /\ phase[r] = "BOUND" /\ proof[r] = "NONE"
                /\ effects' = [effects EXCEPT ![r] = {}]
                /\ proof' = [proof EXCEPT ![r] = "STOPPED"]
                /\ UNCHANGED <<used, owner, phase, unknown, healthy>>
Release(r) == /\ phase[r] \in {"ADMITTED", "BOUND"}
              /\ (proof[r] # "NONE" \/
                  (Fault = "health_release" /\ owner[r] \notin healthy))
              /\ used' = used - 1
              /\ phase' = [phase EXCEPT ![r] = "RELEASED"]
              /\ UNCHANGED <<owner, effects, proof, unknown, healthy>>
FailNode(n) == /\ n \in healthy /\ healthy' = healthy \ {n}
               /\ UNCHANGED <<used, owner, phase, effects, proof, unknown>>
UnsafeReplace(r,n) == /\ Fault = "unknown_replacement" /\ unknown[r]
                      /\ phase[r] = "ADMITTED" /\ n # owner[r]
                      /\ n \notin effects[r]
                      /\ effects' = [effects EXCEPT ![r] = @ \cup {n}]
                      /\ UNCHANGED <<used, owner, phase, proof, unknown, healthy>>
Next == (\E r \in Requests, n \in Nodes : Admit(r,n) \/ UnsafeReplace(r,n))
        \/ (\E r \in Requests : Execute(r) \/ Bind(r) \/ LoseResponse(r)
             \/ RejectBeforeEffect(r) \/ StopProof(r) \/ Release(r))
        \/ (\E n \in Nodes : FailNode(n))

QuotaBounded == used <= Limit
AccountingMatches == used = Cardinality({r \in Requests : phase[r] \in {"ADMITTED", "BOUND"}})
UniqueWritableOwner == \A r \in Requests : Cardinality(effects[r]) <= 1
ReleasedHasProof == \A r \in Requests : phase[r] = "RELEASED" =>
                   (proof[r] # "NONE" /\ effects[r] = {})
Spec == Init /\ [][Next]_vars
====
