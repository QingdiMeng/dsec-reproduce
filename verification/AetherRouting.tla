--------------------------- MODULE AetherRouting ---------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANT Fault
Requests == {"slow", "ready"}
VARIABLES phase, session, stopped, effects
vars == <<phase, session, stopped, effects>>
Init == /\ phase = [r \in Requests |-> "NEW"]
        /\ session = "LIVE" /\ stopped = FALSE /\ effects = 0
Accept(r) == /\ ~stopped /\ phase[r] = "NEW"
             /\ phase' = [phase EXCEPT ![r] = "HEADER"]
             /\ UNCHANGED <<session, stopped, effects>>
FinishHeader(r) == /\ phase[r] = "HEADER"
                  /\ phase' = [phase EXCEPT ![r] = "READY"]
                  /\ UNCHANGED <<session, stopped, effects>>
Eligible(r) == /\ ~stopped /\ session = "LIVE" /\ phase[r] = "READY"
              /\ ~(Fault = "blocking_header" /\ r = "ready" /\ phase["slow"] = "HEADER")
Dispatch(r) == /\ Eligible(r) /\ effects' = effects + 1
               /\ phase' = [phase EXCEPT ![r] = "FORWARDED"]
               /\ UNCHANGED <<session, stopped>>
Complete(r) == /\ phase[r] = "FORWARDED"
              /\ phase' = [phase EXCEPT ![r] = "DONE"]
              /\ UNCHANGED <<session, stopped, effects>>
Close == /\ session = "LIVE" /\ session' = "DEAD"
         /\ UNCHANGED <<phase, stopped, effects>>
Stop == /\ ~stopped /\ stopped' = TRUE /\ UNCHANGED <<phase, session, effects>>
ImplicitRestart == /\ Fault = "implicit_restart" /\ session = "DEAD"
                   /\ session' = "LIVE" /\ UNCHANGED <<phase, stopped, effects>>
Next == (\E r \in Requests : Accept(r) \/ FinishHeader(r) \/ Dispatch(r) \/ Complete(r))
        \/ Close \/ Stop \/ ImplicitRestart
\* Ready request eligibility never depends on another incomplete header.
NoHeadOfLineBlocking == (~stopped /\ session = "LIVE" /\ phase["ready"] = "READY")
                       => Eligible("ready")
\* Check the transition property with a monotone closed-history witness.
VARIABLE closed
InitClosed == Init /\ closed = FALSE
NextClosed == /\ Next /\ closed' = (closed \/ session' = "DEAD")
ClosedIsFinal == closed => session = "DEAD"
Spec == InitClosed /\ [][NextClosed]_<<vars, closed>>
=============================================================================
