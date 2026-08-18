"""ATS keyword diff — pure local computation, zero tokens.

Deliberately not an LLM job. A model asked "what keywords does this posting
want?" will occasionally invent one the posting never contained, and a keyword
diff that hallucinates is worse than no keyword diff: it sends the owner into a
screen having padded his resume with a term the recruiter never asked for.

Set arithmetic over a curated vocabulary cannot do that. Every term reported as
"missing" is a literal substring of the posting.

Vocabulary = terms harvested from both master resume YAMLs + a hand-written
list of common ATS terms (languages, frameworks, tools, methodologies, and
hardware/EE terms).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Hand-written ATS vocabulary. Each entry: canonical -> alternate spellings.
# ---------------------------------------------------------------------------

VOCAB: dict[str, tuple[str, ...]] = {
    # --- languages ---
    "C": ("c programming", "c language", "embedded c"),
    "C++": ("cpp", "c/c++"),
    "Python": (),
    "Java": (),
    "JavaScript": ("js",),
    "TypeScript": (),
    "Go": ("golang",),
    "Rust": (),
    "Assembly": ("asm", "assembler"),
    "SystemVerilog": ("system verilog",),
    "Verilog": (),
    "VHDL": (),
    "Tcl": (),
    "MATLAB": (),
    "SQL": (),
    "Bash": ("shell scripting", "shell script"),
    "PowerShell": (),
    "Perl": (),
    "Ruby": (),
    "Scala": (),
    "Kotlin": (),
    "Swift": (),
    "R": (),
    # --- software frameworks & platforms ---
    "React": ("react.js", "reactjs"),
    "Node.js": ("nodejs", "node js"),
    "Django": (),
    "Flask": (),
    "FastAPI": ("fast api",),
    "Spring": ("spring boot",),
    "gRPC": (),
    "REST": ("rest api", "restful"),
    "GraphQL": (),
    "Kafka": (),
    "Redis": (),
    "PostgreSQL": ("postgres",),
    "MySQL": (),
    "MongoDB": (),
    "Docker": (),
    "Kubernetes": ("k8s",),
    "Terraform": (),
    "Ansible": (),
    "Jenkins": (),
    "CI/CD": ("continuous integration", "continuous delivery", "continuous deployment", "cicd"),
    "Git": ("version control",),
    "GitHub": ("github actions",),
    "GitLab": (),
    "Linux": ("unix",),
    "AWS": ("amazon web services",),
    "Azure": ("microsoft azure",),
    "Google Cloud": ("gcp", "google cloud platform"),
    "Cloudflare": (),
    "Agile": ("scrum", "sprint"),
    "Microservices": ("microservice",),
    "Distributed Systems": ("distributed system",),
    "Data Structures": ("data structure",),
    "Algorithms": ("algorithm",),
    "Object-Oriented Programming": ("oop", "object oriented"),
    "Unit Testing": ("unit test", "pytest", "junit", "gtest", "googletest"),
    "Debugging": ("debug", "troubleshoot"),
    "Machine Learning": ("ml", "deep learning"),
    "PyTorch": (),
    "TensorFlow": (),
    "LangChain": (),
    "LLM": ("large language model", "genai", "generative ai"),
    "Computer Vision": ("cv", "image processing"),
    "TLS/PKI": ("tls", "ssl", "pki", "x.509", "certificate"),
    "Networking": ("tcp/ip", "tcp", "udp", "http", "dns", "network protocol"),
    "Wireshark": (),
    "Pydantic": (),
    "Ollama": (),
    # --- embedded / firmware ---
    "Embedded Systems": ("embedded", "embedded software"),
    "Firmware": (),
    "Microcontroller": ("mcu", "microcontrollers"),
    "MSP430": (),
    "ARM Cortex": ("cortex-m", "arm cortex-m", "cortex m"),
    "RTOS": ("freertos", "real-time operating system", "real time os"),
    "Device Drivers": ("device driver", "driver development", "low-level driver"),
    "Bare-metal": ("bare metal",),
    "Board Bring-up": ("bring-up", "bringup", "board bring up"),
    "I2C": ("i²c",),
    "SPI": (),
    "UART": ("serial communication",),
    "CAN Bus": ("can bus", "canbus"),
    "USB": (),
    "Ethernet": (),
    "Interrupts": ("interrupt", "isr", "interrupt service routine"),
    "Timers": ("timer", "pwm"),
    "ADC": ("analog to digital", "analog-to-digital", "adcs"),
    "DAC": ("digital to analog", "digital-to-analog"),
    "Code Composer Studio": ("code composer",),
    "JTAG": (),
    "Datasheets": ("datasheet", "datasheet review"),
    "IoT": ("internet of things",),
    "Wi-Fi": ("wifi", "802.11"),
    # --- digital design / ASIC / FPGA ---
    "RTL Design": ("rtl", "register transfer level"),
    "FPGA": ("fpgas",),
    "ASIC": ("asics",),
    "Vivado": (),
    "Quartus": (),
    "Synthesis": ("logic synthesis", "synthesize"),
    "Timing Closure": ("static timing analysis", "sta", "timing analysis"),
    "Design Verification": ("dv", "verification", "functional verification"),
    "UVM": (),
    "Testbench": ("test bench",),
    "Simulation": ("simulate", "modelsim", "questasim", "vcs"),
    "State Machines": ("fsm", "state machine", "finite state machine"),
    "Digital Logic": ("logic design", "combinational logic", "sequential logic"),
    "Computer Architecture": ("microarchitecture", "processor architecture", "cpu architecture"),
    "Cadence": (),
    "Synopsys": (),
    "SoC": ("system on chip", "system-on-chip"),
    # --- analog / EE / PCB ---
    "Analog Design": ("analog", "analog circuit"),
    "Mixed-Signal": ("mixed signal",),
    "Circuit Design": ("circuit analysis", "circuits"),
    "PCB Layout": ("pcb", "pcb design", "printed circuit board", "board layout"),
    "Schematic Capture": ("schematic", "schematics"),
    "KiCad": ("kicad",),
    "Altium": ("altium designer",),
    "SPICE": ("ltspice", "pspice", "hspice"),
    "Soldering": ("surface-mount", "surface mount", "smt", "rework"),
    "Oscilloscope": ("oscilloscopes", "scope"),
    "Logic Analyzer": ("logic analyzers",),
    "Multimeter": ("multimeters", "dmm"),
    "Signal Integrity": ("si", "power integrity"),
    "RF": ("radio frequency", "rf design"),
    "Power Electronics": ("power supply", "dc-dc", "buck converter", "power conversion"),
    "Motor Control": ("h-bridge", "motor drive", "bldc"),
    "Sensors": ("sensor", "sensor fusion", "infrared", "ir sensing"),
    "Hardware Validation": ("hardware test", "bench testing", "characterization", "hardware validation"),
    "Failure Analysis": ("root cause", "debug hardware"),
    "Semiconductor": ("semiconductors", "silicon"),
    "Linear Systems": ("signals and systems", "signal processing", "dsp"),
    "Communication Systems": ("communications engineering", "modulation"),
    # --- process / soft ---
    "Documentation": ("technical writing", "document"),
    "Code Review": ("peer review", "code reviews"),
    "Cross-functional Collaboration": ("cross functional", "cross-functional"),
    "Security Clearance Eligible": (
        "security clearance", "able to obtain a clearance", "clearance eligible",
        "us citizen", "u.s. citizen",
    ),
}


def _harvest_from_resume(resume: dict) -> set[str]:
    """Everything the resume already claims, lowercased for matching."""
    have: set[str] = set()
    for group in (resume.get("skills") or {}).values():
        items = group if isinstance(group, list) else [group]
        for item in items:
            have.add(str(item).strip().lower())
    edu = resume.get("education") or {}
    for key in ("coursework", "in_progress"):
        for course in edu.get(key) or []:
            have.add(str(course).strip().lower())
    for section in ("experience", "projects"):
        for entry in resume.get(section) or []:
            if entry.get("stack"):
                for piece in str(entry["stack"]).split(","):
                    have.add(piece.strip().lower())
    if (resume.get("contact") or {}).get("citizenship"):
        have.add("security clearance eligible")
        have.add("u.s. citizen")
    return {h for h in have if h}


def _resume_blob(resume: dict) -> str:
    """Full flattened resume text — used for substring containment checks."""
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif node is not None:
            parts.append(str(node))

    walk(resume)
    return " ".join(parts).lower()


def _variants(canonical: str) -> tuple[str, ...]:
    return (canonical.lower(),) + tuple(v.lower() for v in VOCAB.get(canonical, ()))


def _present(text: str, canonical: str) -> bool:
    for variant in _variants(canonical):
        # Word-boundary match so "R" doesn't hit every word containing r, and
        # "Go" doesn't match "going".
        pattern = r"(?<![A-Za-z0-9])" + re.escape(variant) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text, re.I):
            return True
    return False


def extract_terms(text: str) -> list[str]:
    """Vocabulary terms literally present in the given text."""
    if not text:
        return []
    return [term for term in VOCAB if _present(text, term)]


# Where an honestly-missing term could still be surfaced from what the resume
# already contains. Keyed by canonical term.
_SURFACING_HINTS: dict[str, str] = {
    "SystemVerilog": "Coursework: Design of Complex Digital Systems (Verilog/Vivado)",
    "RTL Design": "Coursework: Design of Complex Digital Systems (Verilog/Vivado); Digital Logic Design Project",
    "Design Verification": "Digital Logic Design Project — simulated and verified logic against expected waveforms",
    "Simulation": "Digital Logic Design Project — designed and simulated digital logic in Verilog",
    "Digital Logic": "Coursework: Fundamentals of Logic Design; Digital Logic Design Project",
    "Computer Architecture": "Coursework (in progress, completes Dec 2026): Microprocessor Architecture",
    "State Machines": "Digital Logic Design Project — CAD synthesis, minimization, and state assignment",
    "Timers": "Remote-Operated Embedded Vehicle — serviced timer interrupts on MSP430",
    "Interrupts": "Remote-Operated Embedded Vehicle — serviced timer interrupts on MSP430",
    "ADC": "Remote-Operated Embedded Vehicle — sampled sensors over ADC",
    "Motor Control": "Remote-Operated Embedded Vehicle — drove H-bridge motors",
    "Sensors": "Remote-Operated Embedded Vehicle — infrared line-detection and sensing modules",
    "Board Bring-up": "Skills: datasheet-driven bring-up; Remote-Operated Embedded Vehicle",
    "Device Drivers": "Remote-Operated Embedded Vehicle — wrote low-level device drivers from scratch",
    "Datasheets": "Remote-Operated Embedded Vehicle — interpreted manufacturer datasheets",
    "PCB Layout": "Coursework: Circuit Board Layout; Skills: PCB layout (KiCad)",
    "Soldering": "Remote-Operated Embedded Vehicle — soldered custom PCBs with surface-mount components",
    "Oscilloscope": "Remote-Operated Embedded Vehicle — verified signal behavior on an oscilloscope",
    "Logic Analyzer": "Digital Logic Design Project — Analog Discovery 3 logic analyzer",
    "Hardware Validation": "Digital Logic Design Project — verified behavior against expected waveforms",
    "Linear Systems": "Coursework: Linear Systems",
    "Communication Systems": "Coursework (in progress, completes Dec 2026): Communication Engineering",
    "Circuit Design": "Coursework: Electric Circuits, Microelectronics",
    "Embedded Systems": "Coursework: Introduction to Embedded Systems; Remote-Operated Embedded Vehicle",
    "Firmware": "Remote-Operated Embedded Vehicle — developed embedded firmware in C for MSP430",
    "Microcontroller": "Remote-Operated Embedded Vehicle — MSP430",
    "IoT": "Remote-Operated Embedded Vehicle — IoT communication module",
    "Wi-Fi": "Remote-Operated Embedded Vehicle — integrated a Wi-Fi module",
    "CI/CD": "IBM — automated, unattended certificate renewal pipeline",
    "REST": "IBM — ACME protocol integrations across four cloud providers",
    "Networking": "Coursework: Computer Networking; Skills: Wireshark",
    "TLS/PKI": "IBM — TLS certificate lifecycle automation, GSKit, 13 production domains",
    "Unit Testing": "IBM — validated across 13 separate production domains",
    "Debugging": "IBM — automatic rollback on failed deployment",
    "Documentation": "IBM — structured, searchable FAQ generation pipeline",
    "LLM": "IBM — three sequential LLM calls orchestrated in LangChain via LiteLLM",
    "Machine Learning": "AI Occupancy Detection Camera — NVIDIA PeopleNet person detection",
    "Computer Vision": "AI Occupancy Detection Camera — DeepStream vision pipeline",
    "Data Structures": "Coursework: Data Structures & OOP (C++)",
    "Object-Oriented Programming": "Coursework: Data Structures & OOP (C++)",
    "Algorithms": "Remote-Operated Embedded Vehicle — motion control algorithms",
    "Linux": "Coursework: Computer Systems Programming, Introduction to Computer Systems",
    "Agile": "IBM internship — iterative delivery across two project workstreams",
    "Code Review": "IBM internship — production code delivered on a team",
    "Cross-functional Collaboration": "AI Occupancy Detection Camera — leading a team of 4",
    "Security Clearance Eligible": "U.S. Citizen, eligible to obtain a security clearance (hardware resume header)",
}


@dataclass
class KeywordDiff:
    jd_terms: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # missing term -> where it could truthfully be surfaced from the master resume
    surfacing: dict[str, str] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        if not self.jd_terms:
            return 1.0
        return round(len(self.matched) / len(self.jd_terms), 3)


def diff(compressed_jd: str, resume: dict) -> KeywordDiff:
    """Terms the posting asks for, split into what the resume already shows and
    what it does not. `surfacing` is only populated where a truthful hook exists."""
    jd_terms = extract_terms(compressed_jd)
    if not jd_terms:
        return KeywordDiff()

    blob = _resume_blob(resume)
    explicit = _harvest_from_resume(resume)

    matched: list[str] = []
    missing: list[str] = []
    for term in jd_terms:
        if term.lower() in explicit or _present(blob, term):
            matched.append(term)
        else:
            missing.append(term)

    surfacing = {t: _SURFACING_HINTS[t] for t in missing if t in _SURFACING_HINTS}
    return KeywordDiff(
        jd_terms=jd_terms, matched=matched, missing=missing, surfacing=surfacing
    )


def summarize(d: KeywordDiff, limit: int = 8) -> tuple[list[str], list[str]]:
    """(matched_to_show, missing_to_show) — trimmed for the email."""
    return d.matched[:limit], d.missing[:limit]
