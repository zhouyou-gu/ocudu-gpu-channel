# Vocabulary

**This file lists the words used in the documents and the code. Use these words and no others.**

> **If two words mean the same thing, keep only the most proper one and replace the other everywhere.**
>
> **The file is self-contained: each description uses only everyday words and words defined earlier in it.**
>
> **Check every description word by word, and recursively: any vague word, and any word that is neither an everyday word nor defined earlier in the file, must be replaced by a concrete one — then re-check the replacement, until every word passes.**

Format: one entry per line, `Word: short description.`

## Nouns

- signal: a value that varies over time to carry information.
- amplitude: the size of a signal, measured from zero.
- phase: a wave's position within its repeating cycle, measured against a reference wave.
- frequency: the number of wave cycles per second.
- sample: a signal's value at one instant.
- IQ sample: a sample stored as numbers I and Q that together give its amplitude and phase.
- IQ stream: IQ samples kept in time order.
- sample rate: the number of samples per second of represented signal time.
- sample index: a sample's position in a specified IQ stream or group of samples.
- sample epoch: the time represented by the starting sample index of an IQ stream.
- batch: a group of consecutive IQ samples processed together.
- slot: an interval that a radio uses to organize when it sends and receives signals.
- buffer: memory that holds data until it is needed.
- ring: a buffer that reuses its space by returning to the beginning after reaching the end.
- sequence number: a sample's increasing write count, independent of where the sample sits in a ring.
- cursor: the sequence number of the next sample to read from a ring.
- window: consecutive samples selected by their starting position and count.
- CPU: the computer's main processor for running program instructions.
- GPU: a processor that runs many calculations at the same time.
- discrete GPU architecture: a computer design in which the CPU and GPU use separate physical memories.
- unified memory architecture: a computer design in which the CPU and GPU share the same physical memory.
- CUDA: NVIDIA's software for running calculations on its GPUs.
- thread: a sequence of program instructions that can run alongside other sequences.
- OS scheduler: the part of the computer's operating software that decides when each thread runs on a CPU.
- thread priority: a thread's stated preference for CPU time under an OS scheduler's rules.
- CPU affinity: the set of CPUs on which a thread is allowed to run.
- FIFO scheduling: Linux rules that run ready threads ahead of normal threads in order of thread priority, with equal priorities keeping arrival order until a thread waits or gives up the CPU.
- nice value: a Linux setting that gives a normal thread more preference for CPU time when lower, or less preference when higher.
- real-time CPU budget: the CPU time Linux allows threads under FIFO scheduling to use during a stated interval.
- kernel: a set of program instructions run on the GPU.
- CUDA stream: a queue that orders GPU calculations and data transfers.
- backend: the part of the program that performs the same calculations on the CPU or GPU.
- pinned memory: computer memory kept at fixed physical locations for GPU transfers or access.
- socket: a program's connection for sending and receiving messages.
- endpoint: the address at which a socket listens or connects.
- ZMQ: the messaging software used to exchange IQ samples between programs.
- port: a named radio connection that sends IQ streams, receives them, or does both.
- TX port: a port through which a radio sends its IQ stream.
- RX port: a port through which a radio receives its IQ stream.
- radio node: a radio's group of ports sharing a sample rate and sample epoch.
- gNB: a radio node serving as a base station in a mobile network.
- UE: a radio node using the mobile network, such as a phone.
- MAC scheduler: the part of a gNB that decides which UE may use each radio resource in a slot.
- channel: the changes a signal undergoes between a TX port and an RX port.
- channel model: calculations that describe a channel's effects on an IQ stream.
- edge: a directed connection that applies a channel model from a TX port to an RX port.
- physical link: the channels from one radio node to another, sharing a time origin and channel settings.
- lane: an edge identified by its TX port and RX port within a physical link.
- topology: the radio nodes, their ports, and the directed connections between them.
- propagation path: a route a signal follows from a TX port to an RX port.
- delay: the time by which a signal or its copy arrives later.
- complex weight: a pair of numbers multiplied with an IQ sample to change its amplitude and phase.
- tap: a delay and complex weight used to form a copy of an IQ stream in a channel model.
- multipath: the arrival of copies of a signal over different propagation paths.
- power: an IQ sample's I squared plus Q squared, averaged over samples when stated.
- path loss: the reduction in signal power as a signal travels between radios.
- fading: changes in signal amplitude and phase caused by changing propagation paths.
- Doppler shift: a change in received frequency caused by movement.
- carrier frequency offset: a mismatch between the reference frequencies used by the sending and receiving radios.
- noise: unwanted random changes added to a signal.
- interference: other signals that affect reception of the desired signal.
- superposition: the addition of channel outputs representing the same receive time.
- receiver model: a channel model applied after the incoming channel outputs have been added together.
- matrix: a table arranged in rows and columns.
- channel matrix: complex weights arranged by RX-port row and TX-port column for a stated tap and time.
- broker: the program that exchanges IQ streams with radio nodes and applies channel models.
- puller: a broker thread that requests IQ samples from a TX port and writes them to a ring.
- producer: a broker thread that selects a common input window and produces the IQ streams for a radio node's RX ports.
- REP worker: a broker thread that answers an RX port's requests using samples already written to its ring.
- pacing: control of sample release times to match the sample rate.
- look-ahead margin: the maximum processed IQ samples held ready for RX-port requests, measured as represented signal time.
- catch-up margin: the maximum represented signal time that pacing may release without waiting after a delay.
- recovery rate: the maximum rate above the sample rate allowed while making up a delay, stated in parts per million.
- latency: elapsed time from a stated input event to its corresponding output event.
- deadline: the latest time a stated operation may finish.
- starvation: a wait for required samples that lasts beyond the stated limit.
- overflow: an attempt to put more data into a buffer than it has room to hold.
- underflow: an attempt to read more data from a buffer than it holds.

## Adjectives

- static: unchanged during the stated interval.
- dynamic: changing during the stated interval.
- fractional: falling between whole sample intervals when describing a delay.
- coherent: having phase differences that stay fixed or change in a known way.
- synthetic: using test programs to supply IQ streams in place of gNB or UE programs.
- live: exchanging IQ streams with running gNB or UE programs, or connected radio equipment.
- real-time: completing each required operation by its deadline.

## Verbs

- align: select samples from different IQ streams that represent the same time.
- stage: place input samples in a buffer for the next calculation or transfer.
- interpolate: estimate a signal's value between its known samples.
- superpose: add channel outputs representing the same receive time.
- pace: release samples according to the sample rate.
- route: send data to the destination named by the topology.
- emulate: apply channel models to IQ streams exchanged with radio nodes.
- check: compare a result against a stated rule and report whether it passes.

## Unmerged Words (temporary memory)

Words still in the documents or the code that need merging into the lists above. Each line: word — where it lives — what to do.

- node / device — `docs/concepts/glossary.md`, `docs/concepts/broker-loop.md`, `include/ocudu_gpu_channel/config.h` — open: identify each occurrence as a port, radio node, or GPU before replacing it.
- slot / batch — `docs/concepts/glossary.md`, `docs/concepts/broker-loop.md`, `src/broker.cpp` — open: identify each occurrence as a radio slot, sample batch, or buffer position before replacing it.
- server thread — `docs/concepts/broker-loop.md`, `docs/concepts/glossary.md` — open: separate the producer's calculations from the REP worker's replies before replacing it.
- links, link_id, link_key, rx_model, devices, radio_nodes, tx_ports, rx_ports — `docs/reference/`, `include/ocudu_gpu_channel/config.h` — keep stored keys; describe their values using the defined words.
- stage_link, process_superposition, DeviceConfig, RadioNodeRuntime, PortRuntime — `src/`, `include/ocudu_gpu_channel/` — keep exact code identifiers; use the defined words in explanations.
- node, device, slot, server thread — `archive/`, `docs/history/`, recorded experiments in `docs/reports/` — retain original wording in frozen evidence, including recorded commands and flags.
