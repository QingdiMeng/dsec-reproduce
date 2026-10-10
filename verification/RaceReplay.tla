------------------------ MODULE RaceReplay ------------------------
EXTENDS RaceInput, Sequences, Naturals
VARIABLE cursor
allvars == <<s, cursor>>
ReplayInit == /\ Init /\ cursor = 1 /\ s = Observed[1]
ReplayStep == /\ cursor < Len(Observed) /\ cursor' = cursor + 1
              /\ (Next \/ UNCHANGED s) /\ s' = Observed[cursor']
ReplayEnd == /\ cursor = Len(Observed) /\ UNCHANGED allvars
ReplayNext == ReplayStep \/ ReplayEnd
TraceConforms == cursor = Len(Observed) \/ ENABLED ReplayStep
ReplaySpec == ReplayInit /\ [][ReplayNext]_allvars
=============================================================================
