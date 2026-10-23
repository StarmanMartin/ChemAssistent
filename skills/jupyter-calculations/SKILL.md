---
name: jupyter-calculations
description: How to run standard computational-chemistry calculations in the user's JupyterLab with pip packages (RDKit, tblite/GFN2-xTB, ASE, PySCF, MACE/ANI, PyXtal, python-docx) - 3D structures, conformers, geometry optimisation, frequencies/IR, free energies, redox potentials, log K/pKa, Fukui indices, UV-Vis/TDDFT, singlet-triplet gaps, NMR shifts, (hyper)polarizabilities, ML potentials, crystal generation, structure enumeration, imaginary-mode elimination, CO2/metal scans, DOCX reports. Use it before writing execute_jupyter code for any such calculation, and to tell the user what is not possible with pip packages.
---

# Calculations in JupyterLab

Everything here runs with `execute_jupyter` in the conversation's kernel, using
pip packages only. Every recipe in `references/` was run and checked against
experiment; the accuracy and run times below are from those tests (one CPU
workstation). Read the reference file before writing code, copy its helpers,
and do not invent other APIs.

## Rules

1. **Check before you compute.** First cell: import what the task needs and
   print the versions. If a package is missing, tell the user which
   `%pip install ...` line from `references/setup.md` is needed and run it only
   after they agree (it changes their Jupyter environment).
2. **Stay within the run time.** One snippet may run at most the user's
   *execution_timeout* (default 300 s). Anything longer: use the background-job
   pattern in `references/setup.md` and poll it in later snippets.
3. **Size limits.** xTB: up to ~200 atoms. DFT (PySCF, def2-SVP): up to ~20
   heavy atoms per single point within minutes; TDDFT/NMR/polarizabilities on
   the small end of that. Say so before starting something bigger.
4. **Inputs and outputs in `exchange`.** Structures come from Chemotion (SMILES,
   molfiles, attachments saved there by `execute_python`) or from the user.
   Save results as files in `exchange` (XYZ/CIF, CSV/JSON, PNG, DOCX), so they can
   go back to Chemotion.
5. **Report method and accuracy.** Every answer names the method
   (e.g. "GFN2-xTB/ALPB(water)", "B3LYP/def2-SVP/PCM") and the expected error
   from the table below. Never present a screening number as a prediction.
6. **Not possible means not possible.** For the rows marked ❌, say what would be
   needed (the program) and offer the closest thing that does work.

## What works

| Task | Status | How (reference) | Tested accuracy / time |
|---|---|---|---|
| SMILES → 3D (organic) | ✅ | RDKit ETKDG + MMFF, then GFN2 (`xtb.md`) | seconds |
| 3D structures of metal complexes | ❌ from SMILES | Architector needs conda-only `xtb-python`; MANTA/DELFIN are not pip packages. Start from a 3D structure the user provides (XYZ/molfile), then optimise with GFN2 (`xtb.md`) | — |
| Conformer sampling | ✅ | RDKit ensemble, GFN2 re-ranking, Boltzmann weights (`xtb.md`). CREST/GOAT: ❌ (binaries) | 1-butanol: 30 embedded, 8 unique; 3 s |
| Geometry optimisation, frequencies, IR | ✅ | GFN2-xTB (tblite) + ASE (`xtb.md`) | acetic acid C=O 1803 cm⁻¹ (exp. ~1790); < 2 s |
| Imaginary-mode elimination | ✅ | displace ± along the mode, re-optimise, repeat (`xtb.md`) | planar NH₃ (971i) fixed in 1 round |
| Free energies (G, 298 K) | ✅ | ASE `IdealGasThermo` on GFN2 frequencies (`xtb.md`) | — |
| Redox potentials | ⚠ DFT only | ΔG(ox/red) with B3LYP/PCM, E = ΔG/F − 4.44 V vs SHE (`dft.md`). **Not with GFN2**: raw GFN2 ionisation energies are several eV off (benzene: 7.4 V instead of 2.5–2.8 V). No OCCUPIER spin-state scan: try the plausible spin states yourself | benzene E_ox 2.49 V (exp. 2.5–2.8); 70–90 s |
| log K / pKa (thermodynamic cycles) | ⚠ | isodesmic exchange with a reference of known pKa, B3LYP/def2-SVPD/PCM on GFN2 geometries (`dft.md`). GFN2 alone: errors of ~5 log units | acetic acid pKa 6.9 (exp. 4.76), error ~2 log units; 47 s |
| Fukui indices | ✅ | N, N±1 charges at fixed geometry, GFN2 (`xtb.md`) | qualitative (carbonyl C/O of acetaldehyde) |
| ESP | ❌ | not covered | — |
| UV-Vis | ⚠ | TDDFT/TDA B3LYP (`dft.md`); needs diffuse functions for Rydberg/bright bands | benzene S₁ 5.6 eV (exp. 4.9), bright band too high with def2-SVP; 95 s |
| Fluorescence / phosphorescence spectra (AFP), E₀₀ | ❌ | needs excited-state optimisation (ORCA ESD) | — |
| ΔE(S−T) | ⚠ | vertical, TDDFT singlets/triplets (`dft.md`); GFN2 ΔSCF only for ranking (`xtb.md`) | benzene T₁ 4.3 eV TDDFT, 4.9 eV GFN2 (exp. vertical ~3.9) |
| TADF screening | ⚠ | rank candidates by GFN2 ΔSCF ΔE(S−T), confirm the best with TDDFT | ranking only |
| ISC/RISC/IC rates, SOC, lifetimes | ❌ | needs ORCA ESD (spin–orbit coupling, vibronic rates) | — |
| NMR shifts | ⚠ | GIAO-HF/def2-SVP shieldings vs TMS (`dft.md`); DFT-NMR in pyscf-properties is broken with PySCF 2.14. Ensemble: Boltzmann-average over conformers | ethanol ¹³C 20.3/54.1 ppm (exp. 18/58); 9 s |
| NMR couplings, CENSO/ANMR | ❌ | needs CREST/CENSO/ORCA | — |
| Polarizability, hyperpolarizability β | ✅ static, ⚠ dynamic α only | pyscf-properties (`dft.md`); frequency-dependent β not available | p-nitroaniline α 83 au, β ≈ 1450 au (B3LYP/6-31G); 90 s static, 8 min with dynamic α: background job |
| ML potentials | ✅ MACE-OFF, MACE-MP, ANI-2x | ASE calculators (`more.md`). CHGNet, M3GNet, AIMNet2, SchNetPack, NequIP, ALIGNN: not tested | ethanol optimisation: 3 s |
| Crystal structure generation | ✅ | PyXtal random molecular crystals by space group, also from SMILES (`more.md`); no lattice-energy ranking (MACE-MP can rank roughly) | < 1 s |
| CO₂ coordination / distance scans | ✅ | place CO₂ around the metal, GFN2 scan (`xtb.md`) | Cu⁺···OCO minimum at 2.0 Å |
| Structure enumeration | ✅ | RDKit reaction SMARTS, forbidden substructures, grid image, PCA map (`more.md`); ligand-wise mutation of metal complexes ❌ | 22 structures in 0.01 s |
| Reports (DOCX) | ✅ | python-docx with tables and figures (`more.md`); save in `exchange`, attach in Chemotion | — |

When a calculation the user asks for is a ❌ row, say so plainly in the chat
before writing any code.
