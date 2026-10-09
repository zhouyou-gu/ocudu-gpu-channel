# Radio topology

(section-22)=

(mimo-nodes)=

## Radio nodes — a radio is not its socket

Everything before this part describes a graph whose vertices are ZMQ endpoint pairs. That is exactly right for a single-antenna radio and exactly wrong for a radio with two. A 2T2R OCUDU gNB opens **four** ZMQ sockets, and the four are not four radios: they carry one PHY's sample stream, split across ports. This part is the overlay that says so.

The vocabulary gains one level. A **port** is what [Topology graph and YAML model](../reference/configuration.md#topology) called a node — a ZMQ endpoint pair, its TX ring, and its puller. A **radio node** owns one or more ports and everything with timing in it: the sample epoch, the source cursors, the single channel call, the throttle, and the output rows. A **physical link** is the directed pair of radio nodes; a **lane** is one `(rx_port, tx_port)` coefficient inside it. A physical link between an `Nt`-port transmitter and an `Nr`-port receiver expands to `Nt × Nr` lanes, and each lane is an ordinary edge of the engine described in Parts III–IV. There is no second channel engine for MIMO: `process_superposition()` takes `Nr` output rows instead of one, and a single-port topology is the `Nt = Nr = 1` case of the same path.

Ports are grouped in YAML under `radio_nodes:`, and **writing order is the matrix index**. There is no suffix parsing and no sorting: the first entry of `tx_ports` is column 0 of `H`, the first entry of `rx_ports` is row 0. Links then name nodes rather than ports.

    radio_nodes:
      - id: gnb0
        tx_ports: [gnb0_p0, gnb0_p1]     # column 0, column 1
        rx_ports: [gnb0_p0, gnb0_p1]     # row 0, row 1
      - id: ue0
        tx_ports: [ue0_p0, ue0_p1]
        rx_ports: [ue0_p0, ue0_p1]

    links:
      - from: gnb0                        # a link connects RADIOS, not sockets
        to: ue0
        model: dl_2x2

Three properties of that schema are load-time rules rather than conventions:

- **Declaration is all-or-nothing.** A topology that declares some radios and leaves others implicit is rejected. Partial declaration would leave the remaining matrix indices decided by parse order rather than by the author.
- **A port belongs to one node, in one role at most.** A port that is a TX port and not an RX port simply gets no REP worker, because a REP worker on a port the producer never writes does not error — it waits forever on an empty ring, which is a silent hang.
- **Siblings must agree** on sample rate, TX timing offset and receiver model. These are properties of a radio, and a per-port disagreement is a topology that cannot mean what it says.

Omitting `radio_nodes:` entirely is still valid and still means what it always meant: every device lowers to an implicit single-port radio, and the resulting `link_key` keeps its pre-MIMO form with no lane suffix. That exception lives in one function, so "a 1×1 topology is byte-identical to before" is a property of the code rather than of a promise.

**Where the resolution is recorded.** The broker prints what it resolved, in matrix order, at startup: `event=radio_node_resolved id=gnb0 tx[0]=gnb0_p0 tx[1]=gnb0_p1 rx[0]=gnb0_p0 rx[1]=gnb0_p1 implicit=false`. Port order is the one thing a reader cannot check from a coefficient table, so it is logged rather than inferred.

(part-vii)=
