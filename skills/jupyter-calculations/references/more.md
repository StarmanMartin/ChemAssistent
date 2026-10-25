# ML potentials, crystals, enumeration, reports

Needs the helpers from `setup.md`.

## ML potentials (ASE calculators)

Same ASE interface as GFN2: swap `atoms.calc` and use `optimise`, `frequencies`.
MACE-OFF: organic molecules (H, C, N, O, F, P, S, Cl, Br, I). MACE-MP:
materials, whole periodic table. ANI-2x: H, C, N, O, F, S, Cl, neutral only.

```python
from mace.calculators import mace_off, mace_mp
from ase.build import bulk

molecule_ = atoms_from_smiles("CCO")
molecule_.calc = mace_off(model="small", device="cpu", default_dtype="float64")
optimise(molecule_, fmax=0.01)
silicon = bulk("Si", "diamond", a=5.43)
silicon.calc = mace_mp(model="small", device="cpu", default_dtype="float64")
{"MACE-OFF ethanol E / eV": round(molecule_.get_potential_energy(), 3),
 "MACE-MP Si E / atom": round(silicon.get_potential_energy() / len(silicon), 3)}
```

```python
import torchani

molecule_ = atoms_from_smiles("CCO")
molecule_.calc = torchani.models.ANI2x().ase()
optimise(molecule_, fmax=0.01)
round(molecule_.get_potential_energy(), 3)
```

Energies of different methods are on different scales: compare only within
one method.

## Random molecular crystals (PyXtal)

Generation only, no ranking by lattice energy. For a rough ranking, relax the
cells with MACE-MP (`ase.filters.FrechetCellFilter`) and compare energies per
molecule.

```python
from pyxtal import pyxtal
from pyxtal.molecule import pyxtal_molecule

crystal = pyxtal(molecular=True)
crystal.from_random(3, 14, ["benzene"], [2])          # space group 14 (P2_1/c), 2 molecules per cell
crystal.to_file(str(exchange / "benzene_P21c.cif"))

from_smiles = pyxtal(molecular=True)
from_smiles.from_random(3, 2, [pyxtal_molecule("CC(=O)O.smi")], [2])   # "<SMILES>.smi"; space group P-1
from_smiles.to_file(str(exchange / "acetic_acid_P-1.cif"))
{"benzene": (crystal.group.symbol, np.round(crystal.lattice.get_para(degree=True), 2).tolist(), crystal.valid),
 "acetic acid": (from_smiles.group.symbol, from_smiles.valid)}
```

## Structure enumeration with reaction SMARTS

Iterative morphing of a seed with user rules, forbidden substructures filtered,
results drawn and mapped in chemical space (PCA of Morgan fingerprints). Rules
act on explicit H: write them as `[c:1][H]>>[c:1]X`.

```python
import matplotlib.pyplot as plt
from rdkit.Chem import Draw, rdFingerprintGenerator

rules = [AllChem.ReactionFromSmarts(s) for s in (
    "[c:1][H]>>[c:1]F", "[c:1][H]>>[c:1]C", "[c:1][H]>>[c:1]O", "[cH:1]>>[n:1]")]
forbidden = [Chem.MolFromSmarts(s) for s in ("[OH]c(F)", "nn")]
population = {Chem.CanonSmiles("c1ccccc1")}
for generation in range(2):
    new = set()
    for smiles in population:
        parent = Chem.AddHs(Chem.MolFromSmiles(smiles))
        for rule in rules:
            for products in rule.RunReactants((parent,)):
                product = products[0]
                try:
                    Chem.SanitizeMol(product)
                    product = Chem.RemoveHs(product)
                except Exception:
                    continue   # rules can produce impossible valences or aromaticity
                if not any(product.HasSubstructMatch(f) for f in forbidden):
                    new.add(Chem.MolToSmiles(product))
    population |= new
molecules = [Chem.MolFromSmiles(s) for s in sorted(population)]
Draw.MolsToGridImage(molecules[:24], molsPerRow=6, subImgSize=(160, 160),
                     legends=[Chem.MolToSmiles(m) for m in molecules[:24]]).save(str(exchange / "enumeration.png"))

generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=1024)
X = np.array([generator.GetFingerprintAsNumPy(m) for m in molecules], float)
X -= X.mean(0)
_, _, vt = np.linalg.svd(X, full_matrices=False)
pcs = X @ vt[:2].T
fig, ax = plt.subplots(figsize=(5, 4))
ax.scatter(pcs[:, 0], pcs[:, 1])
ax.set_xlabel("PC1"); ax.set_ylabel("PC2"); ax.set_title("chemical space (Morgan FP)")
fig.savefig(exchange / "chemical_space.png", dpi=150); plt.show()
(exchange / "enumeration.smi").write_text("\n".join(sorted(population)) + "\n")
len(molecules)
```

## DOCX report

Collect the figures and tables of the conversation into one document in
`exchange`; `execute_python` can then attach it to a Chemotion element.

```python
import docx
from docx.shared import Cm

document = docx.Document()
document.add_heading("Calculation report", 0)
document.add_paragraph("Method: GFN2-xTB (tblite 0.7) geometries; B3LYP/def2-SVP (PySCF) single points.")
table = document.add_table(rows=1, cols=2)
table.style = "Light Grid Accent 1"
table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Structure", "Value"
for name, value in (("benzene", "2.49 V"), ("ethanol", "54.5 ppm")):
    row = table.add_row().cells
    row[0].text, row[1].text = name, value
for figure in sorted(exchange.glob("*.png"))[:4]:
    document.add_paragraph(figure.stem)
    document.add_picture(str(figure), width=Cm(15))
document.save(str(exchange / "report.docx"))
(exchange / "report.docx").stat().st_size
```
