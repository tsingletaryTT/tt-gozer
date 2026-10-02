---
name: gozer-park
description: Use when a supervisor must stop the model server that is using a Tenstorrent board, run something else on the same chips, and bring the server back - holds the lease through the swap, resets the chips in place, and says when to release instead.
---

# Swapping what runs on a board without letting go of it

A model server holds a board. You need that board for something else, such as a test or a
different model, and you want the first server back afterwards. If you `gozer release` in
between, another agent can take the board in the gap, and your restart queues behind them.
Keep the lease for the whole swap.

## Take the lease under your own pid

```bash
gozer acquire --chips 1 --who "orchard:<run-id>" --reason "<what the run is doing>" \
    --owner-pid $$
eval "$(gozer env <lease-id>)"
```

Pass the pid of the supervisor itself, the process that stays alive for the whole run and
starts the servers and tests. In a shell script, `$$` is the script's own pid. Do not pass the
pid of a throwaway shell or pid 1. The owner pid decides which processes count as yours (see
below), and every process on the machine descends from pid 1, so gozer only counts descendants
for owner pids above 1.

gozer judges a lease with an owner pid by that pid's liveness. It stays `CLAIMED` while the owner
lives and nothing has the device open. The 15 minute grace window of a bare `acquire` does not
apply. When the owner exits and no device is open, gozer reaps the lease.

## Start servers and tests from that process

Start every server and every test from the owner process, with the exports from `gozer env`.
Do not wrap them in `gozer run`. It takes its own lease and releases it when the command ends.

gozer treats a device holder that is the owner pid, or a live descendant of it, as the owner's
own work (`HELD`). A holder outside that chain is `HELD-FOREIGN`. A server that daemonizes
(a double fork, `setsid`, `nohup` from a shell that then exits) is re-parented once its parent
exits. It drops out of the owner's chain and shows as `HELD-FOREIGN`, so keep the launching
parent alive.

Treat `HELD-FOREIGN` on your own board as a real alarm: something outside your process tree has
the device open. A process that exits while gozer reads its parent chain can show
`HELD-FOREIGN` for a moment. Run `gozer status` once more before acting on it.

## The swap

1. Stop the first server cleanly. For tt-model that is `tt-model stop`. Run `gozer status`
   until your chips are no longer `HELD`.
2. Reset the chips and keep the lease:

   ```bash
   gozer reset <lease-id>
   ```

   It refuses (exit 15) while a device is open by one of your own processes. gozer cannot see
   other users' processes, so a device that another user has open does not make it refuse.
   Do not pass `--force` unless a person tells you to.
3. Run the other work with the same exports.
4. When it ends, run `gozer reset <lease-id>` again. A failed or hung run can leave the chips
   dirty.
5. Start the first server again with the same exports.

If the board's eth mesh is shared with another tenant, a reset can perturb that tenant, as a
reset on release can. See the eth-neighbour warning in `gozer-keymaster`.

## Exit codes of `gozer reset`

| Code | Meaning | What to do |
|---|---|---|
| `0` | the reset ran and succeeded | continue |
| `13` | no such lease | the lease was released or reaped; take a new one |
| `15` | refused, nothing was reset | a device is open (stop the workload), the unit now belongs to another lease, or the lease record has no units or chips or disagrees with its units (run `gozer status` and take a new lease) |
| `17` | tt-smi ran and failed | the lease is still yours; read the message, try once more, then stop and tell a person |
| `18` | the reset ran, and the lease was then gone or another lease held its chips | do not start a server; run `gozer status` and take a new lease |

## Release for a long phase with no hardware

The board stays unavailable to other agents for as long as you hold it. If the next phase does
not use the chips for a long time, such as an image build or a CPU-only reference run, run

```bash
gozer release <lease-id>
```

and take a new lease when the hardware phase begins. Expect to queue.

## If the supervisor dies

gozer reaps the lease once the owner pid is dead and no device is open. A server that is still
running keeps its device open, so the lease stays and `gozer status` shows `HELD-FOREIGN`.
Do not use `--force` to clear that. Stop the server first, then run `gozer reconcile`.
