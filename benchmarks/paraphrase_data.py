"""Paraphrase-robustness evaluation dataset for the lexical (TF-IDF/BM25) retriever.

Each entry in :data:`FACTS` is a short common-knowledge, subject-relation-object
style statement (PopQA flavour, cf. ``.memplex/benchmarks/data/popqa_real_1000.jsonl``).
Each entry in :data:`QUERIES` is one paraphrase of a fact, annotated with the
coarse lexical-overlap level between the query text and the fact text:

- ``high``: near-verbatim reuse of the fact's content words
  (e.g. "When was the Eiffel Tower completed?" vs "The Eiffel Tower was completed in 1889.").
- ``medium``: synonym substitution and/or sentence restructuring; roughly half of
  the fact's content words survive (e.g. "Which artist created the Mona Lisa?").
- ``low``: indirect question or hypernym/hyponym substitution; (almost) no shared
  content words (e.g. "Which Renaissance polymath produced the famous portrait of
  a smiling woman?").

The default retriever tokenizes on whitespace with no stemming or stopword
removal (``memplex/retrieval/embedding.py`` + the lite FTS5/BM25 sidecar), so
``medium``/``low`` queries are exactly where lexical matching is expected to
degrade — that degradation is what this dataset measures.

The module is pure data + tiny accessors; it must stay importable without any
memplex or third-party dependency so tests can validate dataset integrity cheaply.
"""

from __future__ import annotations

from typing import Any

#: Bumped whenever facts/queries/labels change so baselines stay attributable.
DATASET_VERSION = "1.0.0"

OVERLAP_LEVELS = ("high", "medium", "low")

# ── Facts ─────────────────────────────────────────────────────────────────────
#
# Each fact: {"id": "pf01", "subject": <canonical subject phrase>, "text": <statement>}
# ``subject`` is used by the eval script to filter colliding distractor documents.

FACTS: list[dict[str, str]] = [
    {
        "id": "pf01",
        "subject": "Eiffel Tower",
        "text": "The Eiffel Tower was completed in 1889.",
    },
    {
        "id": "pf02",
        "subject": "Mona Lisa",
        "text": "Leonardo da Vinci painted the Mona Lisa.",
    },
    {
        "id": "pf03",
        "subject": "water boiling point",
        "text": "Water boils at 100 degrees Celsius at sea level.",
    },
    {
        "id": "pf04",
        "subject": "Great Wall of China",
        "text": "The Great Wall of China stretches over 21000 kilometers.",
    },
    {
        "id": "pf05",
        "subject": "Hamlet",
        "text": "William Shakespeare wrote the tragedy Hamlet.",
    },
    {
        "id": "pf06",
        "subject": "Amazon River",
        "text": "The Amazon River flows through Brazil.",
    },
    {
        "id": "pf07",
        "subject": "theory of relativity",
        "text": "Albert Einstein developed the theory of relativity.",
    },
    {
        "id": "pf08",
        "subject": "Mount Everest",
        "text": "Mount Everest is the highest mountain on Earth.",
    },
    {
        "id": "pf09",
        "subject": "speed of light",
        "text": "Light travels at approximately 299792 kilometers per second in a vacuum.",
    },
    {
        "id": "pf10",
        "subject": "Paris",
        "text": "Paris is the capital of France.",
    },
    {
        "id": "pf11",
        "subject": "structure of DNA",
        "text": "James Watson and Francis Crick discovered the structure of DNA.",
    },
    {
        "id": "pf12",
        "subject": "Pacific Ocean",
        "text": "The Pacific Ocean is the largest ocean on Earth.",
    },
    {
        "id": "pf13",
        "subject": "Apollo 11",
        "text": "Apollo 11 landed the first humans on the Moon in 1969.",
    },
    {
        "id": "pf14",
        "subject": "Ninth Symphony",
        "text": "Ludwig van Beethoven composed the Ninth Symphony.",
    },
    {
        "id": "pf15",
        "subject": "photosynthesis",
        "text": "Plants convert sunlight into energy through photosynthesis.",
    },
    {
        "id": "pf16",
        "subject": "Titanic",
        "text": "The Titanic sank in the North Atlantic Ocean in 1912.",
    },
    {
        "id": "pf17",
        "subject": "laws of motion",
        "text": "Isaac Newton formulated the laws of motion and universal gravitation.",
    },
    {
        "id": "pf18",
        "subject": "Sahara",
        "text": "The Sahara is the largest hot desert in the world.",
    },
    {
        "id": "pf19",
        "subject": "World War II",
        "text": "World War II ended in 1945.",
    },
    {
        "id": "pf20",
        "subject": "human heart",
        "text": "The human heart pumps blood through the circulatory system.",
    },
    {
        "id": "pf21",
        "subject": "printing press",
        "text": "The printing press was invented by Johannes Gutenberg around 1440.",
    },
    {
        "id": "pf22",
        "subject": "Mars",
        "text": "Mars is the fourth planet from the Sun.",
    },
    {
        "id": "pf23",
        "subject": "Sydney Opera House",
        "text": "The Sydney Opera House was designed by Jorn Utzon.",
    },
    {
        "id": "pf24",
        "subject": "malaria",
        "text": "Malaria is transmitted to humans by infected Anopheles mosquitoes.",
    },
    {
        "id": "pf25",
        "subject": "gold",
        "text": "Gold has the chemical symbol Au.",
    },
]

# ── Paraphrase queries ────────────────────────────────────────────────────────
#
# Each fact carries exactly 4 queries: one ``high``, one ``low``, plus two more
# split so the strata stay roughly balanced (odd-numbered facts get an extra
# ``medium``, even-numbered facts an extra ``low``).

_QUERIES: list[dict[str, str]] = [
    # pf01 — Eiffel Tower
    {"fact_id": "pf01", "overlap": "high",
     "text": "When was the Eiffel Tower completed?"},
    {"fact_id": "pf01", "overlap": "medium",
     "text": "In what year did builders finish the Eiffel Tower?"},
    {"fact_id": "pf01", "overlap": "medium",
     "text": "Which year marks the end of construction of the Eiffel Tower?"},
    {"fact_id": "pf01", "overlap": "low",
     "text": "When did the French capital's famous iron lattice landmark open to visitors?"},
    # pf02 — Mona Lisa
    {"fact_id": "pf02", "overlap": "high",
     "text": "Who painted the Mona Lisa?"},
    {"fact_id": "pf02", "overlap": "medium",
     "text": "Which artist created the Mona Lisa?"},
    {"fact_id": "pf02", "overlap": "low",
     "text": "Who produced the famous portrait of a smiling woman housed in the Louvre?"},
    {"fact_id": "pf02", "overlap": "low",
     "text": "Which Renaissance polymath is credited with the world's most famous portrait?"},
    # pf03 — water boiling point
    {"fact_id": "pf03", "overlap": "high",
     "text": "At what temperature does water boil at sea level?"},
    {"fact_id": "pf03", "overlap": "medium",
     "text": "At how many degrees Celsius does water start boiling at sea level?"},
    {"fact_id": "pf03", "overlap": "medium",
     "text": "How hot must water get before it boils at sea level?"},
    {"fact_id": "pf03", "overlap": "low",
     "text": "What is the vaporization point of H2O under standard atmospheric pressure?"},
    # pf04 — Great Wall
    {"fact_id": "pf04", "overlap": "high",
     "text": "How many kilometers does the Great Wall of China stretch?"},
    {"fact_id": "pf04", "overlap": "medium",
     "text": "What is the total length of the Great Wall of China?"},
    {"fact_id": "pf04", "overlap": "low",
     "text": "What distance does the ancient Chinese fortification span?"},
    {"fact_id": "pf04", "overlap": "low",
     "text": "What extent does the famous ancient defensive barrier in East Asia cover?"},
    # pf05 — Hamlet
    {"fact_id": "pf05", "overlap": "high",
     "text": "Who wrote the tragedy Hamlet?"},
    {"fact_id": "pf05", "overlap": "medium",
     "text": "Who is the author of Hamlet?"},
    {"fact_id": "pf05", "overlap": "medium",
     "text": "Which playwright composed the play Hamlet?"},
    {"fact_id": "pf05", "overlap": "low",
     "text": "Which English dramatist penned the drama about the Danish prince?"},
    # pf06 — Amazon River
    {"fact_id": "pf06", "overlap": "high",
     "text": "Where does the Amazon River flow?"},
    {"fact_id": "pf06", "overlap": "medium",
     "text": "Through which country does the Amazon River run?"},
    {"fact_id": "pf06", "overlap": "low",
     "text": "Which South American nation contains most of the world's largest rainforest basin?"},
    {"fact_id": "pf06", "overlap": "low",
     "text": "In which country is most of the planet's largest tropical rainforest located?"},
    # pf07 — relativity
    {"fact_id": "pf07", "overlap": "high",
     "text": "Who developed the theory of relativity?"},
    {"fact_id": "pf07", "overlap": "medium",
     "text": "Which physicist came up with the theory of relativity?"},
    {"fact_id": "pf07", "overlap": "medium",
     "text": "What is Albert Einstein's most famous scientific contribution?"},
    {"fact_id": "pf07", "overlap": "low",
     "text": "Who formulated the famous equation linking energy and mass?"},
    # pf08 — Mount Everest
    {"fact_id": "pf08", "overlap": "high",
     "text": "What is the highest mountain on Earth?"},
    {"fact_id": "pf08", "overlap": "medium",
     "text": "Which peak stands tallest on Earth?"},
    {"fact_id": "pf08", "overlap": "low",
     "text": "What is the name of the loftiest summit in the Himalayas?"},
    {"fact_id": "pf08", "overlap": "low",
     "text": "Which Asian summit has the greatest elevation above sea level?"},
    # pf09 — speed of light
    {"fact_id": "pf09", "overlap": "high",
     "text": "How many kilometers per second does light travel in a vacuum?"},
    {"fact_id": "pf09", "overlap": "medium",
     "text": "What is the speed of light in a vacuum?"},
    {"fact_id": "pf09", "overlap": "medium",
     "text": "At roughly what velocity does light propagate through a vacuum?"},
    {"fact_id": "pf09", "overlap": "low",
     "text": "How quickly do photons move through empty space?"},
    # pf10 — Paris
    {"fact_id": "pf10", "overlap": "high",
     "text": "What is the capital of France?"},
    {"fact_id": "pf10", "overlap": "medium",
     "text": "Paris is the capital of which country?"},
    {"fact_id": "pf10", "overlap": "low",
     "text": "Which city hosts the Louvre museum?"},
    {"fact_id": "pf10", "overlap": "low",
     "text": "From which city does the French government operate?"},
    # pf11 — DNA
    {"fact_id": "pf11", "overlap": "high",
     "text": "Who discovered the structure of DNA?"},
    {"fact_id": "pf11", "overlap": "medium",
     "text": "Which scientists uncovered the structure of DNA?"},
    {"fact_id": "pf11", "overlap": "medium",
     "text": "What did Watson and Crick discover?"},
    {"fact_id": "pf11", "overlap": "low",
     "text": "Who first described the double helix molecule?"},
    # pf12 — Pacific Ocean
    {"fact_id": "pf12", "overlap": "high",
     "text": "What is the largest ocean on Earth?"},
    {"fact_id": "pf12", "overlap": "medium",
     "text": "Which ocean covers the biggest area on Earth?"},
    {"fact_id": "pf12", "overlap": "low",
     "text": "Which body of water spans the greatest portion of the planet?"},
    {"fact_id": "pf12", "overlap": "low",
     "text": "Where would you find the widest expanse of salt water?"},
    # pf13 — Apollo 11
    {"fact_id": "pf13", "overlap": "high",
     "text": "When did Apollo 11 land on the Moon?"},
    {"fact_id": "pf13", "overlap": "medium",
     "text": "Which mission first carried humans to the Moon?"},
    {"fact_id": "pf13", "overlap": "medium",
     "text": "What year saw the Apollo 11 Moon landing?"},
    {"fact_id": "pf13", "overlap": "low",
     "text": "In what year did people set foot on our natural satellite?"},
    # pf14 — Ninth Symphony
    {"fact_id": "pf14", "overlap": "high",
     "text": "Who composed the Ninth Symphony?"},
    {"fact_id": "pf14", "overlap": "medium",
     "text": "Which composer wrote the Ninth Symphony?"},
    {"fact_id": "pf14", "overlap": "low",
     "text": "Who created the famous choral work featuring the Ode to Joy?"},
    {"fact_id": "pf14", "overlap": "low",
     "text": "Which classical composer kept writing music after losing his hearing?"},
    # pf15 — photosynthesis
    {"fact_id": "pf15", "overlap": "high",
     "text": "How do plants convert sunlight into energy?"},
    {"fact_id": "pf15", "overlap": "medium",
     "text": "What process lets plants turn sunlight into energy?"},
    {"fact_id": "pf15", "overlap": "medium",
     "text": "How do plants use photosynthesis to make energy?"},
    {"fact_id": "pf15", "overlap": "low",
     "text": "By what mechanism do green organisms produce food from light?"},
    # pf16 — Titanic
    {"fact_id": "pf16", "overlap": "high",
     "text": "What year did the Titanic sink in the North Atlantic Ocean?"},
    {"fact_id": "pf16", "overlap": "medium",
     "text": "In which ocean did the Titanic sink?"},
    {"fact_id": "pf16", "overlap": "low",
     "text": "Which famous shipwreck happened on a maiden voyage from Southampton?"},
    {"fact_id": "pf16", "overlap": "low",
     "text": "What vessel struck an iceberg and was lost in the early twentieth century?"},
    # pf17 — laws of motion
    {"fact_id": "pf17", "overlap": "high",
     "text": "Who formulated the laws of motion?"},
    {"fact_id": "pf17", "overlap": "medium",
     "text": "Which scientist developed the laws of motion and gravitation?"},
    {"fact_id": "pf17", "overlap": "medium",
     "text": "What did Isaac Newton formulate?"},
    {"fact_id": "pf17", "overlap": "low",
     "text": "Who explained why apples fall from trees?"},
    # pf18 — Sahara
    {"fact_id": "pf18", "overlap": "high",
     "text": "What is the largest hot desert in the world?"},
    {"fact_id": "pf18", "overlap": "medium",
     "text": "Which desert is bigger than any other hot desert?"},
    {"fact_id": "pf18", "overlap": "low",
     "text": "Which African region of sand dunes covers an area comparable to a continent?"},
    {"fact_id": "pf18", "overlap": "low",
     "text": "Where is the biggest expanse of subtropical sand on the planet?"},
    # pf19 — World War II
    {"fact_id": "pf19", "overlap": "high",
     "text": "World War II ended in which year?"},
    {"fact_id": "pf19", "overlap": "medium",
     "text": "When did the Second World War finish?"},
    {"fact_id": "pf19", "overlap": "medium",
     "text": "When was the end of World War II?"},
    {"fact_id": "pf19", "overlap": "low",
     "text": "In which year did the global conflict that began in 1939 conclude?"},
    # pf20 — human heart
    {"fact_id": "pf20", "overlap": "high",
     "text": "What pumps blood through the circulatory system?"},
    {"fact_id": "pf20", "overlap": "medium",
     "text": "Which organ pushes blood around the body?"},
    {"fact_id": "pf20", "overlap": "low",
     "text": "What keeps oxygen flowing to every tissue in the body?"},
    {"fact_id": "pf20", "overlap": "low",
     "text": "Which muscular organ maintains circulation in humans?"},
    # pf21 — printing press
    {"fact_id": "pf21", "overlap": "high",
     "text": "Who invented the printing press?"},
    {"fact_id": "pf21", "overlap": "medium",
     "text": "Which German craftsman created the printing press?"},
    {"fact_id": "pf21", "overlap": "medium",
     "text": "What did Johannes Gutenberg invent?"},
    {"fact_id": "pf21", "overlap": "low",
     "text": "Who revolutionized book production in fifteenth-century Europe?"},
    # pf22 — Mars
    {"fact_id": "pf22", "overlap": "high",
     "text": "Which planet is the fourth from the Sun?"},
    {"fact_id": "pf22", "overlap": "medium",
     "text": "What is the fourth planet in our solar system?"},
    {"fact_id": "pf22", "overlap": "low",
     "text": "Which world is known for its reddish appearance?"},
    {"fact_id": "pf22", "overlap": "low",
     "text": "Where would you find Olympus Mons and the Valles Marineris?"},
    # pf23 — Sydney Opera House
    {"fact_id": "pf23", "overlap": "high",
     "text": "Who designed the Sydney Opera House?"},
    {"fact_id": "pf23", "overlap": "medium",
     "text": "Which architect created the Sydney Opera House?"},
    {"fact_id": "pf23", "overlap": "medium",
     "text": "What did Jorn Utzon design?"},
    {"fact_id": "pf23", "overlap": "low",
     "text": "Which Danish architect created Australia's most famous performing arts venue?"},
    # pf24 — malaria
    {"fact_id": "pf24", "overlap": "high",
     "text": "How is malaria transmitted to humans?"},
    {"fact_id": "pf24", "overlap": "medium",
     "text": "Which mosquitoes transmit malaria?"},
    {"fact_id": "pf24", "overlap": "low",
     "text": "What illness do blood-feeding insects spread in tropical regions?"},
    {"fact_id": "pf24", "overlap": "low",
     "text": "Which disease causes cycles of fever and chills after a bite in the tropics?"},
    # pf25 — gold
    {"fact_id": "pf25", "overlap": "high",
     "text": "What is the chemical symbol for gold?"},
    {"fact_id": "pf25", "overlap": "medium",
     "text": "What does the symbol Au represent?"},
    {"fact_id": "pf25", "overlap": "medium",
     "text": "Au is the symbol of which element?"},
    {"fact_id": "pf25", "overlap": "low",
     "text": "Which precious metal did alchemists try to create from lead?"},
]

#: Materialized query list with stable IDs (``pf01_q1`` ... per fact order).
QUERIES: list[dict[str, Any]] = []


def _build_queries() -> None:
    """Assign stable per-fact query IDs and validate referential integrity."""
    fact_ids = {f["id"] for f in FACTS}
    counters: dict[str, int] = {}
    for entry in _QUERIES:
        fact_id = entry["fact_id"]
        if fact_id not in fact_ids:
            raise ValueError(f"query references unknown fact {fact_id!r}")
        if entry["overlap"] not in OVERLAP_LEVELS:
            raise ValueError(f"unknown overlap level {entry['overlap']!r}")
        counters[fact_id] = counters.get(fact_id, 0) + 1
        QUERIES.append(
            {
                "id": f"{fact_id}_q{counters[fact_id]}",
                "fact_id": fact_id,
                "text": entry["text"],
                "overlap": entry["overlap"],
            }
        )


_build_queries()


def fact_by_id(fact_id: str) -> dict[str, str]:
    """Return the fact dict for *fact_id* (raises KeyError when unknown)."""
    for fact in FACTS:
        if fact["id"] == fact_id:
            return fact
    raise KeyError(fact_id)


def queries_by_overlap(level: str) -> list[dict[str, Any]]:
    """Return all queries annotated with overlap *level*."""
    if level not in OVERLAP_LEVELS:
        raise ValueError(f"unknown overlap level {level!r}")
    return [q for q in QUERIES if q["overlap"] == level]
