# Native workspace operation

Use native workflows when running the pinned stacks without the container gate. The native workspace owns external source trees and builds; this repository owns launchers, configuration and patch locks.

## Prepare and inspect

Read the [native workflow index](../../scripts/native/README.md), select the intended platform and workspace root, and verify the source/patch locks before launching. The [platform guide](platforms.md) describes CUDA architecture, memory mode and CPU placement. Do not copy host-specific paths from the historical Korean restart record into a different workspace.

Use the existing native gate matching the radio and antenna shape. Its README defines required binaries, namespaces, configuration and evidence outputs. Keep the broker topology, gNB antenna mappings and UE capability consistent. An srsUE run remains rank-1; an OAI result requires the corresponding OAI gate and revision.

## Restart and cleanup

Stop the processes owned by the selected gate using its recorded PIDs or cleanup procedure. Confirm its sockets and namespaces are released before restarting the same workflow. Preserve unrelated radio services. Start dependencies in the gate's documented order, then inspect registration, PDU sessions and traffic rather than relying on dashboard readiness.

Record the resolved workspace root, binary revisions, patch checksums, platform profile, logs and verdict. A successful restart establishes only that run's behavior. The [historical native Sionna restart record](../history/translations/ko/native-sionna-live-restart.md) retains the original host-specific investigation; it is not a universal run command.

When startup fails, inspect the first failing process and the launcher log before retrying. See [troubleshooting](troubleshooting.md) and [live-stack integration](live-stacks.md).
