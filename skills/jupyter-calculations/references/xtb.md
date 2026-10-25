# GFN2-xTB recipes (tblite + ASE)

Needs the helpers from `setup.md`. GFN2-xTB is fast (seconds) and good for
geometries, frequencies, conformer ranking and trends; it is **not** good for
absolute ionisation/redox energies or anion thermochemistry (see SKILL.md).

## Optimise, IR spectrum

```python
import matplotlib.pyplot as plt

atoms = optimise(xtb(atoms_from_smiles("CC(=O)O")))
print("E / eV:", round(atoms.get_potential_energy(), 4))
with tempfile.TemporaryDirectory() as tmp:
    ir = Infrared(atoms, name=f"{tmp}/ir")
    ir.run()
    freqs, intensities = ir.get_frequencies(), ir.intensities
pairs = sorted(zip(freqs, intensities), key=lambda p: abs(p[0]))[6:]   # internal modes
wavenumbers = np.linspace(400, 4000, 3600)
spectrum = sum(i * np.exp(-((wavenumbers - f.real) / 15) ** 2) for f, i in pairs)
fig, ax = plt.subplots(figsize=(8, 3.5))
ax.plot(wavenumbers, spectrum); ax.invert_xaxis()
ax.set_xlabel("wavenumber / cm$^{-1}$"); ax.set_ylabel("intensity (rel.)"); ax.set_title("IR, GFN2-xTB")
fig.savefig(exchange / "ir_spectrum.png", dpi=150); plt.show()
atoms.write(exchange / "acetic_acid_gfn2.xyz")
[(round(f.real), round(i, 2)) for f, i in sorted(pairs, key=lambda p: -p[1])[:3]]
```

## Imaginary-mode elimination and free energy

`eliminate_imaginary` optimises, checks the internal modes and, while one is
imaginary (beyond the noise `cutoff`), displaces along it in both directions,
keeps the lower energy and re-optimises. Use it before every free energy.

```python
def eliminate_imaginary(atoms, calc_kwargs, max_rounds=5, step=0.3, cutoff=50.0):
    for round_ in range(1, max_rounds + 1):
        optimise(xtb(atoms, **calc_kwargs))
        freqs, modes, _ = frequencies(atoms)
        order = sorted(range(len(freqs)), key=lambda i: abs(freqs[i]))[5 if is_linear(atoms) else 6:]
        imaginary = [(i, freqs[i].imag) for i in order if freqs[i].imag > cutoff]
        if not imaginary:
            return atoms
        i, value = max(imaginary, key=lambda x: x[1])
        mode = modes[i] / np.linalg.norm(modes[i])
        trials = []
        for sign in (1, -1):
            trial = atoms.copy()
            trial.positions += sign * step * mode
            trials.append((xtb(trial, **calc_kwargs).get_potential_energy(), trial))
        print(f"round {round_}: {value:.0f}i cm-1 removed by displacement")
        atoms = min(trials, key=lambda t: t[0])[1]
    raise RuntimeError("still a saddle point after max_rounds")

def gibbs(atoms, charge=0, uhf=0, solvent=None, T=298.15):
    """G in eV (electronic + ZPE + thermal, ideal gas, 1 atm; solvent via ALPB)."""
    kwargs = dict(charge=charge, uhf=uhf, solvent=solvent)
    atoms = eliminate_imaginary(atoms.copy(), kwargs)
    _, _, energies = frequencies(xtb(atoms, **kwargs))
    # small residual noise modes and very soft modes are raised to 50 cm-1 (quasi-RRHO style)
    vib = [max(abs(e), 50 / EV_CM) for e in internal(energies, atoms)]
    thermo = IdealGasThermo(vib_energies=vib, geometry="linear" if is_linear(atoms) else "nonlinear",
                            potentialenergy=atoms.get_potential_energy(), atoms=atoms,
                            symmetrynumber=1, spin=uhf / 2)
    return thermo.get_gibbs_energy(temperature=T, pressure=101325, verbose=False)

planar_nh3 = Atoms("NH3", positions=[[0, 0, 0], [1.01, 0, 0], [-0.505, 0.875, 0], [-0.505, -0.875, 0]])
fixed = eliminate_imaginary(planar_nh3, {})
print("G(NH3) / eV:", round(gibbs(fixed), 4))
```

## Solvation energy

```python
gas = optimise(xtb(atoms_from_smiles("CC(=O)O")))
water = optimise(xtb(gas.copy(), solvent="water"))
print("dG_solv (electronic, eV):", round(water.get_potential_energy() - xtb(water.copy()).get_potential_energy(), 3))
```

## Conformer ensemble

RDKit generates, GFN2 re-optimises and ranks, duplicates are pruned by
heavy-atom RMSD after the GFN2 optimisation.

```python
from rdkit.Chem import rdMolAlign

smiles = "CCCCO"
mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
conf_ids = list(AllChem.EmbedMultipleConfs(mol, numConfs=30, randomSeed=7))
AllChem.MMFFOptimizeMoleculeConfs(mol, maxIters=2000)
results = []
for cid in conf_ids:
    a = optimise(xtb(Atoms([x.GetSymbol() for x in mol.GetAtoms()], positions=mol.GetConformer(cid).GetPositions())), fmax=0.02)
    mol.GetConformer(cid).SetPositions(a.positions)
    results.append((a.get_potential_energy(), cid))
heavy = Chem.RemoveHs(mol)
unique = []
for energy, cid in sorted(results):
    if all(rdMolAlign.GetBestRMS(heavy, heavy, prbId=cid, refId=other) > 0.25 for _, other in unique):
        unique.append((energy, cid))
rel = np.array([e for e, _ in unique]); rel = (rel - rel.min()) * EV_KCAL           # kcal/mol
weights = np.exp(-rel / (0.0019872 * 298.15)); weights /= weights.sum()
with open(exchange / "conformers.xyz", "w") as handle:
    for (energy, cid), w in zip(unique, weights):
        handle.write(Chem.MolToXYZBlock(mol, confId=cid).replace("\n\n", f"\nE={energy:.6f} eV weight={w:.3f}\n", 1))
print(f"{len(conf_ids)} embedded, {len(unique)} unique")
[(round(r, 2), round(w, 3)) for r, w in zip(rel, weights)][:8]
```

## Fukui indices (N, N±1 at fixed geometry)

f⁺ (nucleophilic attack) = q(N) − q(N+1), f⁻ (electrophilic attack) = q(N−1) − q(N).

```python
from tblite.interface import Calculator as TBCalculator

def gfn2_charges(atoms, charge, uhf):
    calc = TBCalculator("GFN2-xTB", atoms.numbers, atoms.positions / 0.52917721, charge=charge, uhf=uhf)
    calc.set("verbosity", 0)
    return calc.singlepoint().get("charges")

molecule_ = optimise(xtb(atoms_from_smiles("CC=O")))
q0, q_plus, q_minus = gfn2_charges(molecule_, 0, 0), gfn2_charges(molecule_, -1, 1), gfn2_charges(molecule_, 1, 1)
fukui = [{"atom": f"{s}{i + 1}", "f+": round(a, 3), "f-": round(b, 3)}
         for i, (s, a, b) in enumerate(zip(molecule_.get_chemical_symbols(), q0 - q_plus, q_minus - q0))]
fukui
```

## CO₂ around a metal centre (distance scan)

Replace the bare ion by the user's complex (`ase.io.read(exchange / "complex.xyz")`),
put CO₂ on the open site and scan; rotate CO₂ with `Atoms.rotate` for angle scans.

```python
metal = Atoms("Cu", positions=[[0, 0, 0]])
scan = []
for d in (1.8, 1.9, 2.0, 2.1, 2.2, 2.4, 2.6, 3.0):
    system = metal + Atoms("OCO", positions=[[d, 0, 0], [d + 1.16, 0, 0], [d + 2.32, 0, 0]])
    scan.append((d, xtb(system, charge=1).get_potential_energy()))
e_min = min(e for _, e in scan)
[(d, round((e - e_min) * EV_KCAL, 1)) for d, e in scan]   # kcal/mol relative to the minimum
```

## Singlet–triplet gap for screening (ΔSCF)

Vertical, at the singlet geometry. GFN2 overestimates (benzene 4.9 eV vs ~3.9);
use it to rank candidates, then check the best with TDDFT (`dft.md`).

```python
singlet = optimise(xtb(atoms_from_smiles("c1ccccc1")))
e_s = singlet.get_potential_energy()
e_t = xtb(singlet.copy(), uhf=2).get_potential_energy()
round(e_t - e_s, 2)
```
