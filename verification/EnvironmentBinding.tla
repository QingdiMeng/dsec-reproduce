---- MODULE EnvironmentBinding ----
EXTENDS TLC
CONSTANTS Requests, Revisions, InitialRevision, Fault
VARIABLES live, observed, phase, selected, accepted
vars == <<live, observed, phase, selected, accepted>>

Init == /\ live = InitialRevision /\ observed = InitialRevision
        /\ phase = [r \in Requests |-> "NONE"]
        /\ selected = [r \in Requests |-> "_"]
        /\ accepted = [r \in Requests |-> "_"]
Probe == /\ observed' = live
         /\ UNCHANGED <<live, phase, selected, accepted>>
Change(v) == /\ v \in Revisions /\ live' = v
             /\ UNCHANGED <<observed, phase, selected, accepted>>
Admit(r) == /\ phase[r] = "NONE"
            /\ selected' = [selected EXCEPT ![r] = observed]
            /\ phase' = [phase EXCEPT ![r] = "ADMITTED"]
            /\ UNCHANGED <<live, observed, accepted>>
NodeCheck(r) == /\ phase[r] = "ADMITTED" /\ live = selected[r]
                /\ phase' = [phase EXCEPT ![r] = "CHECKED"]
                /\ UNCHANGED <<live, observed, selected, accepted>>
Send(r) == /\ phase[r] = "CHECKED"
           /\ phase' = [phase EXCEPT ![r] = "DISPATCHED"]
           /\ UNCHANGED <<live, observed, selected, accepted>>
EdgeAccept(r) == /\ phase[r] = "DISPATCHED"
                 /\ (live = selected[r] \/ Fault = "missing_edge_guard")
                 /\ accepted' = [accepted EXCEPT ![r] = live]
                 /\ phase' = [phase EXCEPT ![r] = "BOUND"]
                 /\ UNCHANGED <<live, observed, selected>>
Reject(r) == /\ phase[r] \in {"ADMITTED", "DISPATCHED"}
             /\ live # selected[r]
             /\ phase' = [phase EXCEPT ![r] = "REJECTED"]
             /\ UNCHANGED <<live, observed, selected, accepted>>
Next == Probe \/ (\E v \in Revisions : Change(v)) \/
        (\E r \in Requests : Admit(r) \/ NodeCheck(r) \/ Send(r) \/ EdgeAccept(r) \/ Reject(r))

AcceptedRevisionPinned == \A r \in Requests : accepted[r] # "_" => accepted[r] = selected[r]
RejectedHasNoEffect == \A r \in Requests : phase[r] = "REJECTED" => accepted[r] = "_"
Spec == Init /\ [][Next]_vars
====
