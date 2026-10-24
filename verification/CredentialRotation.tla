---- MODULE CredentialRotation ----
EXTENDS TLC
CONSTANT Fault
VARIABLES phase, oldValid, newValid, completionProof
vars == <<phase,oldValid,newValid,completionProof>>
Init == /\ phase = "BASE" /\ oldValid = TRUE /\ newValid = FALSE /\ completionProof = "none"
Prepare == /\ phase = "BASE" /\ oldValid
           /\ phase' = "OVERLAP" /\ newValid' = TRUE
           /\ UNCHANGED <<oldValid,completionProof>>
Complete(p) == /\ phase = "OVERLAP" /\ newValid
               /\ (p = "new" \/ Fault = "unproved")
               /\ phase' = "COMPLETED" /\ oldValid' = FALSE
               /\ completionProof' = p /\ UNCHANGED newValid
Cancel == /\ ((phase = "OVERLAP" /\ oldValid) \/ (Fault = "stale_cancel" /\ phase = "COMPLETED"))
          /\ phase' = "CANCELLED" /\ newValid' = FALSE
          /\ UNCHANGED <<oldValid,completionProof>>
Next == Prepare \/ Complete("old") \/ Complete("new") \/ Cancel
NoCredentialLockout == oldValid \/ newValid
CompletionHasNewProof == phase = "COMPLETED" => completionProof = "new"
Spec == Init /\ [][Next]_vars
====
