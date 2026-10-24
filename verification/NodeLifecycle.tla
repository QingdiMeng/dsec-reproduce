---- MODULE NodeLifecycle ----
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Requests, MaxVersion, Fault
VARIABLES phase, version, admitted, released, live, stopped, held,
          proofVersion, savedResume, everRetired, admissionPhases
vars == <<phase, version, admitted, released, live, stopped, held,
          proofVersion, savedResume, everRetired, admissionPhases>>

Init == /\ phase = "JOINING" /\ version = 1
        /\ admitted = {} /\ released = {} /\ live = {} /\ stopped = {}
        /\ held = 0 /\ proofVersion = 0 /\ savedResume = 0
        /\ everRetired = FALSE
        /\ admissionPhases = [r \in Requests |-> "NONE"]

Activate == /\ phase = "JOINING" /\ version < MaxVersion
            /\ phase' = "ACTIVE" /\ version' = version + 1
            /\ UNCHANGED <<admitted, released, live, stopped, held, proofVersion,
                           savedResume, everRetired, admissionPhases>>
Admit(r) == /\ r \notin admitted
            /\ (phase = "ACTIVE" \/ (Fault = "stale_admit" /\ phase = "DRAINING"))
            /\ admitted' = admitted \cup {r} /\ held' = held + 1
            /\ admissionPhases' = [admissionPhases EXCEPT ![r] = phase]
            /\ UNCHANGED <<phase, version, released, live, stopped, proofVersion, savedResume, everRetired>>
Dispatch(r) == /\ r \in admitted \ (released \cup stopped \cup live)
               /\ live' = live \cup {r}
               /\ UNCHANGED <<phase, version, admitted, released, stopped, held,
                              proofVersion, savedResume, everRetired, admissionPhases>>
StopProof(r) == /\ r \in admitted \ (released \cup stopped)
                /\ live' = live \ {r} /\ stopped' = stopped \cup {r}
                /\ UNCHANGED <<phase, version, admitted, released, held,
                               proofVersion, savedResume, everRetired, admissionPhases>>
Release(r) == /\ r \in stopped \ released
              /\ released' = released \cup {r} /\ held' = held - 1
              /\ UNCHANGED <<phase, version, admitted, live, stopped,
                             proofVersion, savedResume, everRetired, admissionPhases>>
Drain == /\ phase = "ACTIVE" /\ version < MaxVersion
         /\ phase' = "DRAINING" /\ version' = version + 1
         /\ UNCHANGED <<admitted, released, live, stopped, held, proofVersion,
                        savedResume, everRetired, admissionPhases>>
InspectEmpty == /\ phase \in {"DRAINING", "MAINTENANCE"} /\ live = {}
                /\ proofVersion' = version
                /\ UNCHANGED <<phase, version, admitted, released, live, stopped,
                               held, savedResume, everRetired, admissionPhases>>
Finish(target) == /\ phase \in {"DRAINING", "MAINTENANCE"}
                  /\ (target = "RETIRED" \/ phase = "DRAINING")
                  /\ version < MaxVersion /\ proofVersion = version
                  /\ (held = 0 \/ Fault = "missing_held")
                  /\ phase' = target /\ version' = version + 1
                  /\ everRetired' = (everRetired \/ (target = "RETIRED"))
                  /\ UNCHANGED <<admitted, released, live, stopped, held,
                                 proofVersion, savedResume, admissionPhases>>
SaveResume == /\ phase = "MAINTENANCE" /\ savedResume = 0
              /\ savedResume' = version
              /\ UNCHANGED <<phase, version, admitted, released, live, stopped,
                             held, proofVersion, everRetired, admissionPhases>>
Resume == /\ savedResume > 0 /\ version < MaxVersion
          /\ ((phase = "MAINTENANCE" /\ version = savedResume) \/ Fault = "stale_transition")
          /\ phase' = "ACTIVE" /\ version' = version + 1
          /\ UNCHANGED <<admitted, released, live, stopped, held,
                         proofVersion, savedResume, everRetired, admissionPhases>>
Next == Activate \/ Drain \/ InspectEmpty \/ SaveResume \/ Resume
        \/ Finish("MAINTENANCE") \/ Finish("RETIRED")
        \/ (\E r \in Requests : Admit(r) \/ Dispatch(r) \/ StopProof(r) \/ Release(r))

AdmissionRequiresActive == \A r \in admitted : admissionPhases[r] = "ACTIVE"
HeldMatches == held = Cardinality(admitted \ released)
QuiescentRequiresNoWork == phase \in {"MAINTENANCE", "RETIRED"} => held = 0 /\ live = {}
RetiredIsFinal == everRetired => phase = "RETIRED"
Spec == Init /\ [][Next]_vars
====
