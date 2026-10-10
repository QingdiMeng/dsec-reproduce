------------------------- MODULE ShutdownReplay -------------------------
EXTENDS ShutdownSignal, Sequences, Naturals
CONSTANT Observed
VARIABLE cursor
allvars == <<stop, signalled, phase, returned, cursor>>

ReplayInit == /\ Init /\ cursor = 1 /\ State = Observed[1]
ReplayStep == /\ cursor < Len(Observed) /\ cursor' = cursor + 1
              /\ Next /\ State' = Observed[cursor']
ReplayEnd == /\ cursor = Len(Observed) /\ UNCHANGED allvars
ReplayNext == ReplayStep \/ ReplayEnd
\* Check transitions against the original spec; never copy its transition
\* rules into Python. A prefix with no legal next observed state is rejected.
TraceConforms == cursor = Len(Observed) \/ ENABLED ReplayStep
ReplaySpec == ReplayInit /\ [][ReplayNext]_allvars
=============================================================================
