# Vendored Binaries

## ectool

**What:** ChromeOS Embedded Controller tool — communicates with Framework Laptop's
EC firmware to read sensors, control fan duty, adjust thermal thresholds, etc.

**Source:** https://gitlab.howett.net/DHowett/ectool
**License:** BSD 3-Clause (ChromiumOS)
**Commit:** `b6308da644e` ([CI job #899](https://gitlab.howett.net/DHowett/ectool/-/jobs/899))
**Artifact:** [download link](https://gitlab.howett.net/DHowett/ectool/-/jobs/899/artifacts/download?file_type=archive)
**Compiler:** Debian clang 14.0.6 (Bookworm), x86-64 Linux
**Linked against:** libftdi1, libusb-1.0 (dynamically linked)
**SHA256:** `db51252568d36c93591396e0ff425f9352bf39186bc716de9d4de56acb98877a`
**License text:** See [LICENSE](https://gitlab.howett.net/DHowett/ectool/-/blob/main/LICENSE)
(BSD 3-Clause, reproduced in the source repo)

**Why vendored:** The binary is only 301K. Vendoring it ensures `install-ectool.sh`
has a known-good fallback if the source build fails (missing deps, network issues,
etc.). The script tries to build from source first for full transparency.

**To reproduce from source:**

    git clone https://gitlab.howett.net/DHowett/ectool.git
    cd ectool && git checkout b6308da644e
    git submodule update --init --recursive
    mkdir _build && cd _build
    CC=clang CXX=clang++ cmake -GNinja ..
    cmake --build .
    # Binary: src/ectool (inside _build/)
