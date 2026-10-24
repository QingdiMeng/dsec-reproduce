---- MODULE TransportRotation ----
EXTENDS Naturals, TLC
CONSTANT Fault
VARIABLES phase, connectedPhase, peer, acceptedPhase, acceptedPeer,
          reading, applied, highest
vars == <<phase,connectedPhase,peer,acceptedPhase,acceptedPeer,reading,applied,highest>>
Trusted(p) == CASE p = 0 -> {"old"} [] p = 1 -> {"old","new"} [] OTHER -> {"new"}
Init == /\ phase = 0 /\ connectedPhase = 0 /\ peer = "none"
        /\ acceptedPhase = 0 /\ acceptedPeer = "none"
        /\ reading = 0 /\ applied = 0 /\ highest = 0
Rotate == /\ phase < 2 /\ phase' = phase + 1
          /\ UNCHANGED <<connectedPhase,peer,acceptedPhase,acceptedPeer,reading,applied,highest>>
Connect(p) == /\ p \in Trusted(phase) /\ connectedPhase' = phase /\ peer' = p
              /\ UNCHANGED <<phase,acceptedPhase,acceptedPeer,reading,applied,highest>>
Authorize == /\ peer # "none" /\ (connectedPhase = phase \/ Fault = "stale_peer")
             /\ acceptedPhase' = phase /\ acceptedPeer' = peer
             /\ UNCHANGED <<phase,connectedPhase,peer,reading,applied,highest>>
Read == /\ reading' = phase
        /\ UNCHANGED <<phase,connectedPhase,peer,acceptedPhase,acceptedPeer,applied,highest>>
Apply == \E candidate \in {0,reading} :
         /\ (candidate >= highest \/ Fault = "stale_publication")
         /\ applied' = candidate /\ highest' = IF candidate > highest THEN candidate ELSE highest
         /\ UNCHANGED <<phase,connectedPhase,peer,acceptedPhase,acceptedPeer,reading>>
Next == Rotate \/ Connect("old") \/ Connect("new") \/ Authorize \/ Read \/ Apply
TrustedAtAcceptance == acceptedPeer = "none" \/ acceptedPeer \in Trusted(acceptedPhase)
PublicationNeverRegresses == applied = highest
Spec == Init /\ [][Next]_vars
====
