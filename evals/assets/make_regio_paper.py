"""Write regio-3b.pdf, the attachment of the eval task "paper-structure".

A page in the style of a supporting information: the text names compounds 2b
and 3b with the same formula and m/z, and only the drawing (an image, no text
layer) shows which isomer is which. A model that reads only the text has to
guess the substitution pattern; one that looks at the scheme can read it.

Needs RDKit and Pillow, which the app does not (a throwaway venv will do):

    python3 -m venv /tmp/rdkit && /tmp/rdkit/bin/pip install rdkit pillow
    /tmp/rdkit/bin/python evals/assets/make_regio_paper.py

It prints the InChIKey of 3b that tasks.json checks against.
"""
from __future__ import annotations

import zlib
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import Draw, rdMolDescriptors

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "regio-3b.pdf"
# 2b: 3-bromo-4-methoxybenzaldehyde, the common isomer a guess would give;
# 3b: 2-bromo-5-methoxybenzaldehyde, the one the task asks for.
COMPOUNDS = {"2b": "COc1ccc(C=O)cc1Br", "3b": "O=Cc1cc(OC)ccc1Br"}
PROTON = 1.007276


def escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def main() -> None:
    mols = {label: Chem.MolFromSmiles(smiles) for label, smiles in COMPOUNDS.items()}
    formulas = {rdMolDescriptors.CalcMolFormula(mol) for mol in mols.values()}
    assert len(formulas) == 1, formulas  # the text must not tell them apart
    mz = rdMolDescriptors.CalcExactMolWt(mols["3b"]) + PROTON
    ion = "C8H8BrO2"

    options = Draw.MolDrawOptions()
    options.legendFontSize = 28
    picture = Draw.MolsToGridImage(list(mols.values()), molsPerRow=2, subImgSize=(420, 320),
                                   legends=list(mols), drawOptions=options).convert("RGB")
    width, height = picture.size

    lines = [
        ("Supporting Information (excerpt)", 14),
        ("", 11),
        ("General procedure A. The aryl bromide (5.0 mmol) was formylated as described in", 11),
        ("the main text; the products are shown in Scheme S1.", 11),
        ("", 11),
        (f"Compound 2b: colourless solid, 1.02 g (66%). HRMS (ESI) m/z [M+H]+ calcd for {ion}", 11),
        (f"{mz:.4f}, found {mz + 0.0003:.4f}.", 11),
        (f"Compound 3b: pale yellow solid, 1.21 g (78%). HRMS (ESI) m/z [M+H]+ calcd for {ion}", 11),
        (f"{mz:.4f}, found {mz - 0.0003:.4f}.", 11),
        ("", 11),
        ("Scheme S1. Products of general procedure A.", 11),
    ]
    text = b"BT /F1 11 Tf 56 780 Td 15 TL "
    for line, size in lines:
        text += b"/F1 %d Tf (" % size + escape(line) + b") ' "
    text += b"ET "
    # The scheme below the text, 480 pt wide.
    shown_width, shown_height = 480, round(480 * height / width)
    text += b"q %d 0 0 %d 56 %d cm /Scheme Do Q" % (shown_width, shown_height, 560 - shown_height)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> /XObject << /Scheme 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pixels = zlib.compress(picture.tobytes(), 9)
    objects.append(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                   b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % (width, height, len(pixels))
                   + pixels + b"\nendstream")
    objects.append(b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    OUTPUT.write_bytes(bytes(out))
    print(f"wrote {OUTPUT} ({len(out)} bytes)")
    for label, mol in mols.items():
        print(f"{label}: {COMPOUNDS[label]}  InChIKey {Chem.MolToInchiKey(mol)}")


if __name__ == "__main__":
    main()
