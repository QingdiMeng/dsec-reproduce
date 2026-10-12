------------------------------ MODULE QemuCID -------------------------------
EXTENDS Naturals, FiniteSets
CONSTANT Fault
Owners == 1..3
CIDs == 1..2
VARIABLES leases, assigned, live, stopped, released
vars == <<leases, assigned, live, stopped, released>>
Init == /\ leases = [c \in CIDs |-> 0]
        /\ assigned = [o \in Owners |-> 0]
        /\ live = {} /\ stopped = {} /\ released = {}
Reserve(o,c) == /\ assigned[o] = 0
                /\ (leases[c] = 0 \/ Fault = "unlocked_reserve")
                /\ leases' = [leases EXCEPT ![c] = o]
                /\ assigned' = [assigned EXCEPT ![o] = c]
                /\ UNCHANGED <<live, stopped, released>>
Spawn(o) == /\ assigned[o] # 0 /\ leases[assigned[o]] = o
            /\ o \notin live \cup stopped
            /\ live' = live \cup {o}
            /\ UNCHANGED <<leases, assigned, stopped, released>>
ProveDeath(o) == /\ assigned[o] # 0 /\ o \notin stopped
                 /\ live' = live \ {o} /\ stopped' = stopped \cup {o}
                 /\ UNCHANGED <<leases, assigned, released>>
Retire(o) == /\ assigned[o] # 0 /\ o \notin released
             /\ (o \in stopped \/ Fault = "early_release")
             /\ (leases[assigned[o]] = o \/ Fault = "stale_token")
             /\ leases' = [leases EXCEPT ![assigned[o]] = 0]
             /\ released' = released \cup {o}
             /\ UNCHANGED <<assigned, live, stopped>>
(* Delayed duplicate cleanup after this owner's successful release. *)
StaleRetire(o) == /\ Fault = "stale_token" /\ o \in released
                  /\ leases[assigned[o]] # 0
                  /\ leases' = [leases EXCEPT ![assigned[o]] = 0]
                  /\ UNCHANGED <<assigned, live, stopped, released>>
Next == (\E o \in Owners, c \in CIDs: Reserve(o,c))
        \/ (\E o \in Owners: Spawn(o) \/ ProveDeath(o) \/ Retire(o) \/ StaleRetire(o))
LiveHasLease == \A o \in live: leases[assigned[o]] = o
NoLiveCIDCollision == \A a,b \in live: a # b => assigned[a] # assigned[b]
ReleasedHasDeathProof == released \subseteq stopped
Spec == Init /\ [][Next]_vars
=============================================================================
