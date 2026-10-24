# Setup, shared helpers, long jobs

## Packages

Check first; install only after the user agreed (one `%pip` line per group):

```python
import importlib
for name in ("rdkit", "ase", "tblite", "pyscf", "pyxtal", "docx", "mace", "torchani", "matplotlib", "numpy"):
    try:
        module = importlib.import_module(name)
        print(name, getattr(module, "__version__", "ok"))
    except ImportError:
        print(name, "MISSING")
```

| Group | Install line |
|---|---|
| structures, xTB | `%pip install rdkit ase tblite matplotlib` |
| DFT | `%pip install pyscf pyscf-properties` |
| crystals, reports | `%pip install pyxtal python-docx` |
| MACE | `%pip install torch --index-url https://download.pytorch.org/whl/cpu` then `%pip install mace-torch` |
| ANI-2x | `%pip install torchani`; on Python 3.14 its dependency numcodecs does not build: `%pip install --no-deps torchani huggingface_hub` |

MACE and ANI download their model weights on first use (MACE-MP: 31 MB into
`~/.cache/mace` on the Jupyter server). Restart nothing: new packages import in
the running kernel.

Python 3.14 note: all packages above have wheels except torchani's numcodecs.
Not pip-installable at all: xtb-python (conda), CREST, CENSO, ORCA.

## Helpers (paste once per kernel)

All later recipes use these. Units: ASE works in eV and Å; PySCF in Hartree
and Bohr internally (`gto.M` takes Å).

```python
import tempfile, warnings
warnings.filterwarnings("ignore")
import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem
from ase import Atoms
from ase.optimize import BFGS
from ase.vibrations import Vibrations, Infrared
from ase.thermochemistry import IdealGasThermo
from tblite.ase import TBLite

RDLogger.DisableLog("rdApp.*")
HARTREE_EV = 27.211386
EV_KCAL = 23.0605
EV_CM = 8065.54

def atoms_from_smiles(smiles, seed=42):
    """3D structure of an organic molecule: RDKit ETKDG + MMFF."""
    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    AllChem.EmbedMolecule(mol, randomSeed=seed)
    AllChem.MMFFOptimizeMolecule(mol)
    return Atoms([a.GetSymbol() for a in mol.GetAtoms()], positions=mol.GetConformer().GetPositions())

def xtb(atoms, charge=0, uhf=0, solvent=None):
    """Attach GFN2-xTB. uhf = number of unpaired electrons; solvent e.g. "water", "acetonitrile" (ALPB)."""
    kwargs = dict(method="GFN2-xTB", charge=charge, multiplicity=uhf + 1, verbosity=0)
    if solvent:
        kwargs["solvation"] = ("alpb", solvent)
    atoms.calc = TBLite(**kwargs)
    return atoms

def optimise(atoms, fmax=0.005, steps=1000):
    BFGS(atoms, logfile=None).run(fmax=fmax, steps=steps)
    return atoms

def is_linear(atoms):
    return len(atoms) == 2 or atoms.get_moments_of_inertia().min() < 1e-3

def frequencies(atoms):
    """Frequencies (cm-1, complex: imaginary = negative curvature), modes, energies (eV).
    The vibration cache goes to a temporary folder, not into exchange."""
    with tempfile.TemporaryDirectory() as tmp:
        vib = Vibrations(atoms, name=f"{tmp}/vib")
        vib.run()
        return vib.get_frequencies(), [vib.get_mode(i) for i in range(3 * len(atoms))], vib.get_energies()

def internal(values, atoms):
    """Drop the 6 (linear: 5) translations/rotations: the values closest to zero."""
    return sorted(values, key=abs)[5 if is_linear(atoms) else 6:]
```

## Long jobs (longer than one snippet may run)

Start them with `start_jupyter_job`, not `execute_jupyter`. The job runs in a
new kernel of its own, may take hours or days, and you get a message with its
outcome when it ends:

- The code must be self-contained: its imports, the definitions of the helpers
  from this file it uses (copy them into the job code), its input read from
  `exchange` (save structures as XYZ/JSON there first, e.g. with
  `execute_jupyter`) and every result saved into `exchange` with a clear name.
- Print progress: the printed output is the job's log; `jupyter_jobs` shows
  its latest lines while the job runs, the report the end of it.
- The report also has the value of the last expression and the new files.
  Figures the job shows are saved as `<job id>_figure_<n>.png`; better save
  them yourself with a clear name.
- After starting it, end your answer and say what runs; do not poll.
