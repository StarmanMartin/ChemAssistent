# DFT recipes (PySCF + pyscf-properties)

Needs the helpers from `setup.md` and, for geometries, `optimise`/`xtb` (GFN2
geometries are good enough for single points). Times are for small molecules
(≤ 12 heavy atoms, def2-SVP); they grow steeply with size (rule 3 in SKILL.md).
Long runs: use the background-job pattern.

```python
from pyscf import gto, dft, scf, tddft

def to_pyscf(atoms, charge=0, spin=0, basis="def2-svp"):
    """spin = number of unpaired electrons (2S)."""
    return gto.M(atom=[(s, p) for s, p in zip(atoms.get_chemical_symbols(), atoms.positions)],
                 basis=basis, charge=charge, spin=spin, verbose=0)

def run_dft(mol, xc="b3lyp", eps=None):
    """RKS/UKS; eps = dielectric constant for PCM (water 78.36, acetonitrile 35.69, DMSO 46.7, DCM 8.93)."""
    mf = dft.RKS(mol) if mol.spin == 0 else dft.UKS(mol)
    mf.xc = xc
    if eps is not None:
        mf = mf.PCM()
        mf.with_solvent.eps = eps
    mf.kernel()
    if not mf.converged:
        raise RuntimeError("SCF did not converge")
    return mf
```

## Redox potential

E(ox) vs SHE = [G(ox) − G(red)] / F − 4.44 V (absolute SHE). Electronic part
from DFT with PCM; add GFN2 thermal corrections (`gibbs` in `xtb.md` minus the
GFN2 energy) when the spin/geometry change is large. For open-shell metal
complexes compute the plausible spin states and use the lowest.

```python
eps_mecn = 35.69
neutral = optimise(xtb(atoms_from_smiles("c1ccccc1")))
cation = optimise(xtb(neutral.copy(), charge=1, uhf=1))
e_red = run_dft(to_pyscf(neutral), eps=eps_mecn).e_tot
e_ox = run_dft(to_pyscf(cation, charge=1, spin=1), eps=eps_mecn).e_tot
round((e_ox - e_red) * HARTREE_EV - 4.44, 2)   # V vs SHE; benzene: 2.49 (exp. 2.5-2.8)
```

## log K / pKa by an isodesmic cycle

Never compute absolute pKa (proton solvation energy). Exchange the proton (or
ligand) with a reference of known pKa / log K: HA + B⁻ → A⁻ + HB,
log K = −ΔG / (RT ln 10), pKa(HA) = pKa(HB) − log K. Expect ~2 log units error.

```python
def g_dft(smiles, charge, eps=78.36):
    """B3LYP/def2-SVPD/PCM energy on the GFN2/ALPB geometry + GFN2 thermal correction, eV."""
    atoms = optimise(xtb(atoms_from_smiles(smiles), charge=charge, solvent="water"))
    e_xtb = atoms.get_potential_energy()
    _, _, energies = frequencies(atoms)
    vib = [max(abs(e), 50 / EV_CM) for e in internal(energies, atoms)]
    thermo = IdealGasThermo(vib, "linear" if is_linear(atoms) else "nonlinear", e_xtb, atoms=atoms,
                            symmetrynumber=1, spin=0)
    correction = thermo.get_gibbs_energy(298.15, 101325, verbose=False) - e_xtb
    return run_dft(to_pyscf(atoms, charge, basis="def2-svpd"), eps=eps).e_tot * HARTREE_EV + correction

G = {s: g_dft(s, c) for s, c in (("CC(=O)O", 0), ("CC(=O)[O-]", -1), ("OC=O", 0), ("[O-]C=O", -1))}
dG = (G["CC(=O)[O-]"] + G["OC=O"]) - (G["CC(=O)O"] + G["[O-]C=O"])
log_k = -dG * EV_KCAL / (0.0019872 * 298.15 * np.log(10))
pka_reference = 3.75   # formic acid
{"log K": round(log_k, 2), "pKa (acetic acid)": round(pka_reference - log_k, 2)}   # exp. 4.76
```

## UV-Vis and singlet/triplet excitations (TDA-TDDFT)

def2-SVP has no diffuse functions: low valence states are fine, bright
high-energy bands come out too high (benzene 154 nm vs 180 nm). Use
`basis="def2-svpd"` or `"aug-cc-pvdz"` for those, at higher cost.

```python
import matplotlib.pyplot as plt

geometry = optimise(xtb(atoms_from_smiles("c1ccccc1")))
mf = run_dft(to_pyscf(geometry))
singlets = tddft.TDA(mf); singlets.nstates = 6; singlets.kernel()
energies_ev, strengths = singlets.e * HARTREE_EV, singlets.oscillator_strength()
triplets = tddft.TDA(mf); triplets.singlet = False; triplets.nstates = 2; triplets.kernel()
t1 = triplets.e[0] * HARTREE_EV

wavelengths = np.linspace(150, 450, 600)
photon_ev = 1239.84 / wavelengths
absorbance = sum(f * np.exp(-((photon_ev - e) / 0.25) ** 2) for e, f in zip(energies_ev, strengths))
fig, ax = plt.subplots(figsize=(7, 3.5))
ax.plot(wavelengths, absorbance)
ax.set_xlabel("wavelength / nm"); ax.set_ylabel("absorbance (rel., σ = 0.25 eV)"); ax.set_title("UV-Vis, TDA-B3LYP/def2-SVP")
fig.savefig(exchange / "uv_vis.png", dpi=150); plt.show()
{"S1 / eV": round(energies_ev[0], 2), "T1 / eV": round(t1, 2), "dE(S1-T1) / eV": round(energies_ev[0] - t1, 2),
 "states": [(round(e, 2), round(f, 3)) for e, f in zip(energies_ev, strengths)]}
```

## NMR chemical shifts (GIAO)

DFT-NMR (`nmr.RKS`) in pyscf-properties fails with PySCF 2.14 (assertion in
`block_loop`); GIAO-HF works. Reference TMS at the same level; average
chemically equivalent nuclei (e.g. the three methyl H). For an ensemble,
Boltzmann-average the shifts over the conformers from `xtb.md`.

```python
from pyscf.prop import nmr

def isotropic_shieldings(atoms, basis="def2-svp"):
    mf = scf.RHF(to_pyscf(atoms, basis=basis)); mf.kernel()
    return np.array([np.trace(t) / 3 for t in nmr.RHF(mf).kernel()])

tms = optimise(xtb(atoms_from_smiles("C[Si](C)(C)C")))
sigma_tms = isotropic_shieldings(tms)
reference = {el: sigma_tms[[i for i, s in enumerate(tms.get_chemical_symbols()) if s == el]].mean() for el in ("H", "C")}

molecule_ = optimise(xtb(atoms_from_smiles("CCO")))
sigma = isotropic_shieldings(molecule_)
[(f"{s}{i + 1}", round(reference[s] - x, 2)) for i, (s, x) in enumerate(zip(molecule_.get_chemical_symbols(), sigma))
 if s in reference]   # ethanol 13C: 20.3 / 54.1 ppm (exp. 18 / 58)
```

## Polarizability and first hyperpolarizability

Static α and β (coupled-perturbed KS); frequency-dependent α only. Atomic units
(β: 1 au = 8.639e-33 esu). p-nitroaniline: ~90 s for α and β, ~8 min with the
dynamic α: over the default run time, so start it as a background job
(`setup.md`).

```python
from pyscf.prop import polarizability

pna = optimise(xtb(atoms_from_smiles("Nc1ccc(cc1)[N+](=O)[O-]")))
mf = run_dft(to_pyscf(pna, basis="6-31g"))
response = polarizability.rks.Polarizability(mf)
alpha = response.polarizability()
beta = response.hyper_polarizability()
beta_vector = [sum(beta[i, j, j] for j in range(3)) for i in range(3)]
alpha_1064 = response.polarizability_with_freq(freq=0.0428)   # 1064 nm in Hartree
{"alpha_iso / au": round(np.trace(alpha) / 3, 1), "alpha_iso(1064 nm) / au": round(np.trace(alpha_1064) / 3, 1),
 "|beta_vec| / au": round(float(np.linalg.norm(beta_vector)), 1)}
```
