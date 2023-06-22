import os, sys
import inspect
from pathlib import Path
import tempfile
from .utils import run_command

import rdkit
from rdkit.Chem import AllChem, DataStructs
from rdkit.Chem import AllChem as Chem
from rdkit.Chem import RDConfig
sys.path.append(os.path.join(RDConfig.RDContribDir, 'SA_Score'))
import sascorer

import numpy as np
import torch
import torch.nn as nn


# Surrogate model for Jsc
def gaussian(x, A, B):
    return A * np.exp(-x** 2 / B)

def get_properties(smile, verbose=False, scratch: str='/tmp'): 
    '''
    Return fitness functions for the design of OPV molecules.

    Args:
        smile: `str` representing molecule
        verbose: `bool` turn on print statements for debugging
        scratch: `str` temporary directory

    Returns:
        pce_pcbm_sas: `float` PCE PCBM minus SAS
        pce_pcdtbt_sas: `float` PCE PCDTBT minus SAS
    '''

    # Create and switch to temporary directory
    owd = Path.cwd()
    scratch_path = Path(scratch)
    tmp_dir = tempfile.TemporaryDirectory(dir=scratch_path)
    os.chdir(tmp_dir.name)

    # Create mol object
    mol = Chem.MolFromSmiles(smile)
    mol = Chem.AddHs(mol)
    if mol == None: 
        return "INVALID"
    charge = Chem.rdmolops.GetFormalCharge(mol)
    atom_number = mol.GetNumAtoms()

    sas = sascorer.calculateScore(mol)
    
    with open('test.smi', 'w') as f: 
        f.writelines([smile])

    system = lambda x: run_command(x, verbose)
    
    # Prepare the input file: 
    system('obabel test.smi --gen3D -O test.xyz')

    # Run the preliminary xtb: 
    command_pre = 'CHARGE={};xtb {} --gfn 0 --opt normal -c $CHARGE --iterations 4000'.format(charge, 'test.xyz')
    system(command_pre)
    system("rm ./gfnff_charges ./gfnff_topo")

    # Run crest conformer ensemble
    command_crest = 'CHARGE={};crest {} -gff -mquick -chrg $CHARGE --noreftopo'.format(charge, 'xtbopt.xyz')
    system(command_crest)
    system('rm ./gfnff_charges ./gfnff_topo')
    system('head -n {} crest_conformers.xyz > crest_best.xyz'.format(atom_number+2))

    # Run the calculation: 
    command = 'CHARGE={};xtb {} --opt normal -c $CHARGE --iterations 4000 > out_dump'.format(charge, 'crest_best.xyz')
    system(command)

    # Read the output: 
    with open('./out_dump', 'r') as f: 
        text_content = f.readlines()

    output_index = [i for i in range(len(text_content)) if 'Property Printout' in text_content[i]]
    text_content = text_content[output_index[0]: ]
    homo_data = [x for x in text_content if '(HOMO)' in x]
    lumo_data = [x for x in text_content if '(LUMO)' in x]
    homo_lumo_gap = [x for x in text_content if 'HOMO-LUMO GAP' in x]
    mol_dipole    = [text_content[i:i+4] for i,x in enumerate(text_content) if 'molecular dipole:' in x]
    lumo_val      = float(lumo_data[0].split(' ')[-2])
    homo_val = float(homo_data[0].split(' ')[-2])
    homo_lumo_val  = float(homo_lumo_gap[0].split(' ')[-5])
    mol_dipole_val = float(mol_dipole[0][-1].split(' ')[-1])

    # Determine value of custom function for optimization
    HL_range_rest = homo_lumo_val # Good range for the HL gap: 0.8856-3.2627 
    if 0.8856 <= HL_range_rest <= 3.2627: 
        HL_range_rest = 1.0
    elif HL_range_rest < 0.8856: 
        HL_range_rest = 0.1144 + homo_lumo_val
    else: 
        HL_range_rest = 4.2627 - HL_range_rest
    combined = mol_dipole_val + HL_range_rest - lumo_val # Maximize this function
    
    # Compute calibrated homo and lumo levels
    homo_cal = homo_val * 0.8051030400316004 + 2.5376777453204133
    lumo_cal = lumo_val * 0.8787863933542347 + 3.7912767464357200

    # Define parameters for Scharber model
    A = 433.11633173034136
    B = 2.3353220382662894
    Pin = 900.1393292842149

    # Scharber model objective 1: Optimization of donor for phenyl-C61-butyric acid methyl ester (PCBM) acceptor
    voc_1 = (abs(homo_cal) - abs(-4.3)) - 0.3
    if voc_1 < 0.0:
        voc_1 = 0.0
    lumo_offset_1 = lumo_cal + 4.3
    if lumo_offset_1 < 0.3:
        pce_1 = 0.0
    else:
        jsc_1 = gaussian(lumo_cal - homo_cal, A, B)
        if jsc_1 > 415.22529811760637:
            jsc_1 = 415.22529811760637
        pce_1 = 100 * voc_1 * 0.65 * jsc_1 / Pin

    # Scharber model objective 2: Optimization of acceptor for poly[N-90-heptadecanyl-2,7-carbazole-alt-5,5-(40,70-di-2-thienyl-20,10,30-benzothiadiazole)] (PCDTBT) donor
    voc_2 = (abs(-5.5) - abs(lumo_cal)) - 0.3
    if voc_2 < 0.0:
        voc_2 = 0.0
    lumo_offset_2 = -3.6 - lumo_cal
    if lumo_offset_2 < 0.3:
        pce_2 = 0.0
    else:
        jsc_2 = gaussian(lumo_cal - homo_cal, A, B)
        if jsc_2 > 415.22529811760637:
            jsc_2 = 415.22529811760637
        pce_2 = 100 * voc_2 * 0.65 * jsc_2 / Pin

    os.chdir(owd)
    tmp_dir.cleanup()

    # assign values
    pce_pcbm_sas = pce_1 - sas
    pce_pcdtbt_sas = pce_2 - sas

    return pce_pcbm_sas, pce_pcdtbt_sas


def get_fingerprint(smile, nBits, ecfp_degree=2):
    m1 = Chem.MolFromSmiles(smile)
    fp = AllChem.GetMorganFingerprintAsBitVect(m1,ecfp_degree, nBits=nBits)
    x = np.zeros((0, ), dtype=np.int8)
    DataStructs.ConvertToNumpyArray(fp, x)

    return x


if __name__ == '__main__':
    # calculate fitness values
    smi = 'c1ccccc1'
    pce_pcbm_sas, pce_pcdtbt_sas = get_properties(smi)
    print(f'PCE_PCBM - SAS: {pce_pcbm_sas}')
    print(f'PCE_PCDTBT - SAS: {pce_pcdtbt_sas}')


