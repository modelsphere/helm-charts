# mooncake-master

Highly available [Mooncake](https://github.com/kvcache-ai/Mooncake) Store masters.
Two or more `mooncake_master` replicas run with `--enable_ha` and elect a leader
through etcd. The leader serves; the standbys take over when its lease expires.
The chart can bundle a three-member etcd for the election. Optionally, a sidecar
re-registers NVMe-oF volumes on whichever master currently leads.

## Install

```sh
helm repo add modelsphere https://modelsphere.github.io/helm-charts
helm install mooncake modelsphere/mooncake-master -n mooncake --create-namespace
```

The install notes print the address that clients must use. Clients are **not**
given a master address. They are given the HA backend instead, and they follow
the leader through it across failovers:

```sh
MOONCAKE_MASTER=etcd://mooncake-mooncake-master-etcd-client.mooncake.svc:2379
```

To use an etcd you already run, disable the bundled one:

```yaml
etcd:
  enabled: false
ha:
  connstring: http://etcd-0:2379;http://etcd-1:2379;http://etcd-2:2379
```

## Requirements

**The Mooncake build must include etcd HA, on both sides.** `mooncake_master` and
every client that uses `etcd://` must be built with `-DSTORE_USE_ETCD=ON`. Upstream
release builds include it, and so does the default image (`lmsysorg/sglang`, whose
upstream Mooncake wheel puts `mooncake_master` on PATH).

A build without it rejects HA at start-up:

```
Failed to parse HA backend config: UNAVAILABLE_IN_CURRENT_MODE, backend_type=etcd
```

Custom builds default it to OFF. `-DSTORE_USE_ETCD` and `-DSTORE_USE_K8S_LEASE`
cannot be enabled together, because both link a Go c-shared library.

## How it behaves

Verified on a two-master install, with the bundled etcd and the default image.

- **Only the leader opens the RPC port.** A standby keeps it closed, so the RPC port
  doesn't tell you whether a pod is healthy. The chart's probes use the metrics
  port, which both roles open. If the probes used the RPC port, every standby
  would stay NotReady and a rolling update would stall on the first standby it
  replaced.
- **Finding the leader.** It is the pod whose RPC port accepts a connection. Its log
  also says `Master Admin Metrics: role=leader, state=serving`. No exported metric
  tells the roles apart. In particular, `ha_standby_state` reads `0` on the leader
  and on the standby alike.
- **`--rpc_address` is the pod IP.** The leader publishes this address through
  etcd. The RPC port binds to it only, so loopback is refused even on the leader.
- **Failover time is mostly the termination grace period.** `mooncake_master` does
  not exit promptly on SIGTERM. It holds the leadership until it is killed. With
  Kubernetes' default grace period of 30 s, deleting the leader left the cluster
  without a leader for about 39 s. The chart sets
  `master.terminationGracePeriodSeconds: 10`, which measured 20 s from deletion to
  the new leader serving. The recreated pod may win the next election: the
  StatefulSet recreates it under the same name, so compare pod UIDs, not names,
  when you check a failover.

## NVMe-oF volumes (`nofRegistrar`)

Mooncake builds with NVMe-oF support can place KV data on NVMe-oF volumes that
are registered with the master. Upstream builds have no NVMe-oF support.

Registration lives **only in the leader's memory**. After a failover, the new
leader has an empty segment table. The data is still on the volumes, but nothing
knows where, and client reads fail with no hint why.

`nofRegistrar.enabled=true` adds a sidecar to every master pod. In each cycle,
the sidecar checks whether its own master leads (RPC port open). If it leads and
the reported NVMe-oF capacity is short of the volume list, the sidecar registers
the volumes. It never unregisters. Unregistration is open to any caller, and a
volume removed by mistake takes down every client that uses it.

```yaml
master:
  privileged: true        # SPDK
  rdmaDevices: true       # /dev/infiniband
  hugepages: {enabled: true, size: 2Gi}
nofRegistrar:
  enabled: true
  volumes:
    - {nqn: nqn.2004-12.com.example:array1-vol0, traddr: 192.0.2.10, port: 4420, size: 2199023255552}
```

`nofRegistrar.dryRun=true` keeps the leader/standby decision but only logs what
would be registered. Use it to check a deployment against volumes another master
still owns.

**Hugepages.** The SPDK environment that an NVMe-oF client starts cannot
initialise without hugetlbfs: it fails with `EAL: Cannot get hugepage information`.
The amount it needs for itself is small, about 20 MB at start-up. Clients that
hand their host KV pool to Mooncake's allocator (an SGLang HiCache L2 pool, for
example) need the **whole pool** in hugepages as well, because SPDK can only
translate hugepage-backed memory.

**Privileged pods.** A privileged pod sees every GPU on its node, whatever the
device plugin allocated. Keep that in mind when masters share nodes with GPU
workloads.

## Values

`values.yaml` is commented and is the reference. The main keys:

| key | default | |
|---|---|---|
| `image.repository` / `image.tag` | `lmsysorg/sglang` / `v0.5.21` | any image with an etcd-enabled `mooncake_master` |
| `master.replicas` | `2` | must be at least 2 |
| `master.clusterId` | `mooncake` | separates election keys when masters of several clusters share one etcd |
| `master.hostNetwork` | `true` | gives the master a stable, node-routable address, and lets it reach storage networks |
| `master.runtimeClassName` | `""` | set this on CDI clusters when the Mooncake build links `libcuda`, and add `NVIDIA_VISIBLE_DEVICES=all` to `master.env`; this injects the driver without requesting a GPU |
| `master.terminationGracePeriodSeconds` | `10` | see failover time above |
| `ha.connstring` | `""` | external etcd endpoints; empty means the bundled etcd |
| `etcd.enabled` | `true` | three-member etcd, one member per node |
| `etcd.nodePort.enabled` | `false` | exposes etcd on every node, for clients outside Kubernetes |
| `nofRegistrar.enabled` | `false` | see above |
| `serviceMonitor.enabled` | `false` | scrapes the masters' metrics port |
