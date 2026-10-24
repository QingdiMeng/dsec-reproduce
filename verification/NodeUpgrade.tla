---- MODULE NodeUpgrade ----
EXTENDS Naturals, TLC
CONSTANT Fault
VARIABLES phase, pending, observed, committed, version, completionProof
vars == <<phase,pending,observed,committed,version,completionProof>>
Init == /\ phase = "MAINTENANCE" /\ pending = FALSE
        /\ observed = "old" /\ committed = "old"
        /\ version = 0 /\ completionProof = "old"
Prepare == /\ phase = "MAINTENANCE" /\ ~pending /\ version = 0
           /\ pending' = TRUE /\ version' = 1
           /\ UNCHANGED <<phase,observed,committed,completionProof>>
Start(r) == /\ pending /\ phase = "MAINTENANCE" /\ observed # r
            /\ observed' = r
            /\ UNCHANGED <<phase,pending,committed,version,completionProof>>
Complete(r) == /\ pending /\ phase = "MAINTENANCE"
               /\ (observed = r \/ Fault = "unproved")
               /\ pending' = FALSE /\ committed' = r
               /\ version' = 2 /\ completionProof' = observed
               /\ UNCHANGED <<phase,observed>>
Resume == /\ phase = "MAINTENANCE" /\ version > 0
          /\ (~pending \/ Fault = "pending_resume")
          /\ observed = committed
          /\ phase' = "ACTIVE"
          /\ UNCHANGED <<pending,observed,committed,version,completionProof>>
Next == Prepare \/ Start("old") \/ Start("new")
        \/ Complete("old") \/ Complete("new") \/ Resume
CompletionHasLoadedProof == ~pending => completionProof = committed
PendingBlocksAdmission == phase = "ACTIVE" => ~pending
Spec == Init /\ [][Next]_vars
====
