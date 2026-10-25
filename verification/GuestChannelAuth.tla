------------------------- MODULE GuestChannelAuth -------------------------
EXTENDS Naturals, TLC
CONSTANT Fault
VARIABLE s
vars == <<s>>
Keys == {1, 2}
Ports == {5001, 5002}
Nonces == {1, 2}
Proofs == [key : Keys, port : Ports, nonce : Nonces]
Init == s = [phase |-> "NEW", nonce |-> 0, used |-> {}, port |-> 5001,
              proof |-> [key |-> 2, port |-> 5002, nonce |-> 1],
              effect |-> FALSE, valid |-> TRUE]
Challenge(n, p) == /\ s.phase = "NEW" /\ n \notin s.used
                  /\ s' = [s EXCEPT !.phase = "CHALLENGE", !.nonce = n,
                            !.port = p, !.used = @ \cup {n}]
Present(proof) == /\ s.phase = "CHALLENGE"
                 /\ s' = [s EXCEPT !.phase = "PRESENTED", !.proof = proof]
Valid == s.proof.key = 1 /\ s.proof.port = s.port /\ s.proof.nonce = s.nonce
Accept == /\ s.phase = "PRESENTED"
          /\ (Fault = "bypass" \/
               (s.proof.key = 1 /\ (Fault = "port" \/ s.proof.port = s.port)
                               /\ (Fault = "nonce" \/ s.proof.nonce = s.nonce)))
          /\ s' = [s EXCEPT !.phase = "AUTHENTICATED", !.valid = Valid]
Dispatch == /\ s.phase = "AUTHENTICATED"
            /\ s' = [s EXCEPT !.phase = "DONE", !.effect = TRUE]
Close == /\ s.phase \in {"CHALLENGE", "PRESENTED", "AUTHENTICATED", "DONE"}
         /\ s' = [s EXCEPT !.phase = "NEW", !.effect = FALSE, !.valid = TRUE]
Next == (\E n \in Nonces, p \in Ports: Challenge(n,p))
        \/ (\E proof \in Proofs: Present(proof)) \/ Accept \/ Dispatch \/ Close
EffectsRequireBoundProof == s.effect => s.valid
Spec == Init /\ [][Next]_vars
=============================================================================
