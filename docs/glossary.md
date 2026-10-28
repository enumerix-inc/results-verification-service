# Glossary

Precise shared language for the results verification service project. When in doubt, refer here.

---

**Run**
A single execution of the test harness. One run contains one or more experiments. Identified by a unique `run_id` generated at runtime. A run produces a build verdict.

**Experiment**
A scientific/analytical unit defined by a fixed sample set and a specific feature being tested. An experiment is identified by a stable `experiment_id` that persists across runs, enabling longitudinal tracking. One workbook maps to one experiment.

**Experiment ID**
A stable string identifier for an experiment (e.g. `EXP_LT_control_assay`). Does not change between runs. This is the longitudinal key — it links the same experiment across many builds over time.

**Feature set**
The pipeline capability being exercised by an experiment (e.g. `baseline_algo_performance`, `linkage`, `dynamic_range`). One feature set maps to one experiment in the MVP.

**Gold standard**
A known-good set of expected results for a specific experiment, produced from a validated reference run. Stored as a CSV file and registered into the verification database. Treated as immutable once registered.

**Gold standard version**
A specific registered state of a gold standard for a given experiment. Each time a gold standard is updated, a new version is created. Old versions are never deleted — they are retired by setting `superseded_at`. Every sample result in the database is anchored to the exact gold standard version it was compared against.

**Release**
An optional convenience snapshot recording which gold standard versions were active across all experiments at a given point in time. A release is a lens, not a load-bearing structure — nothing in the verification logic depends on it. Useful for answering "what were the standards on date X?"

**Run context manifest**
A JSON file written by the test harness at the start of a run. It is the contract between the test harness and the verification service. Contains run metadata, experiment definitions, rules, tolerances, and pointers to gold standard references. The verification service reads this file to know what to check and how strictly.

**Stability experiment**
An experiment testing code that should not change between builds. All stability experiments must pass for a build to receive a passing verdict. A failure here means something broke.

**Exploratory experiment**
An experiment testing code under active development (e.g. ML classifier, non-uniformity metrics). Results are tracked longitudinally but deviations do not fail the build verdict. Used to monitor trends in features that are intentionally changing.

**Pre-verification gate**
A mandatory check phase that runs before any results comparison. Consists of two sequential checks: (1) checksum integrity — confirming the gold standard file has not been modified since registration; (2) subset validity — confirming all samples in a child experiment's workbook exist in the parent gold standard. If either check fails the run aborts with a clear reason.

**Build verdict**
The overall pass/fail outcome of a run. Determined by the `build_verdict_policy` in the manifest. In the MVP: all stability experiments must pass. Exploratory experiment failures produce warnings only.

**Workbook**
An Excel file generated at image acquisition time by the main app. Acts as a record of which samples were included in a run and what options were enabled (reference sample, linkage analysis, etc.). The verification service uses it only for sample metadata — it is not a source of expected values.

**Checksum**
A SHA-256 hash of a gold standard CSV file, computed and stored at registration time. Recomputed at the start of every run to confirm the file has not been modified. A mismatch causes the run to abort.

**Listener**
The entry point of the verification service. Polls the pipeline database at a fixed interval for completed runs. Because the regression machine is isolated, every run detected is by definition a test run — no filtering required.

**Acceptance criteria comparison**
An aggregate-scope comparison (`type: acceptance_criteria`) in an experiment's `comparisons` list that judges a metric computed over groups of samples (linearity exponent, linear slope, plateauing ratio, CV, NTC mean) against absolute limits instead of a gold standard (ADR-026). Each criterion records its lower/upper bound and whether each bound is inclusive. The release decision stays with a person reading the report.

**Determinism canary**
A check (`python -m src.validation.canary`) that two stored results of the same experiment from the same IAP build have identical observations and aggregate values. As of IAP v3.0 reanalysis is deterministic within a build, which is what makes a failing acceptance criterion a real failure rather than noise. It is meaningless across builds.

**Sample annotation**
A sidecar YAML file, one per experiment_id, that assigns each sample a group, role and expected counts per channel using ordered regex rules over SampleName (ADR-026). Strict: every sample must match exactly one rule. Expected counts: an integer = assay with that many copies, `0` = assay with no template (no-template control), `null` = no assay on that channel. Its SHA-256 and `annotation_version` are recorded with every result. Never written into the workbook's GroupName.

**Orchestrator**
The central coordinator of the verification service. Loads the manifest, runs the pre-verification gate, delegates per-sample comparisons, hands results to the LLM module, and stores the final report.

**LLM analysis module**
The component responsible for sending structured verification results to the Claude API and receiving a human-readable narrative in return. Separate from the orchestrator because it handles interpretation, not logic.

**Longitudinal tracking**
The ability to query how a metric for a given experiment or sample has changed across multiple builds over time. Enabled by the stable `experiment_id` and the `gs_exp_version_id` FK on all result records.
