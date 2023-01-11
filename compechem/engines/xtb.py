import os, copy, sh, shutil
from typing import Dict
from tempfile import mkdtemp
from compechem.config import get_ncores
from compechem.core.base import Engine
from compechem.systems import System
from compechem.tools import process_output
from compechem.tools import add_flag
from compechem.tools import dissociation_check
from compechem.tools import cyclization_check
from compechem.core.dependency_finder import locate_xtb

import logging

logger = logging.getLogger(__name__)


class XtbInput(Engine):
    """Interface for running xTB calculations"""

    def __init__(
        self,
        method: str = "gfn2",
        solvent: str = None,
        optionals: str = "",
        XTBPATH: str = None,
    ) -> None:
        """
        Parameters
        ----------
        method : str, optional
            level of theory, by default "gfn2"
        solvent : str, optional
            ALPB solvent, by default no solvent (vacuum)
        optionals : str, optional
            optional keywords/flags, by default ""
        XTBPATH: str, optional
            the path to the xtb executable. If set to None (default) the xtb executable will
            be loaded automatically.
        """

        super().__init__(method)
        self.level_of_theory += f" | solvent: {solvent}"

        self.solvent = solvent
        self.optionals = optionals
        self.__XTBPATH = XTBPATH if XTBPATH else locate_xtb()

    def write_input(
        self,
        job_info: Dict,
    ) -> None:

        input = f"$chrg {job_info['charge']}\n"
        input += f"$spin {job_info['spin']-1}\n"

        input += "$write\n"
        input += "   spin population=true"
        if job_info["save_cubes"]:
            input += "   density=true"
            input += "   spin density=true"
            input += "$cube\n"
            input += f"   step={job_info['cube_step']}"

        input += "$end"

        with open("input.inp", "w") as inp:
            inp.writelines(input)

        return

    def spe(
        self,
        mol: System,
        ncores: int = None,
        maxcore=None,
        charge: int = None,
        spin: int = None,
        save_cubes: bool = False,
        cube_step: float = 0.1,
        inplace: bool = False,
        remove_tdir: bool = True,
    ):
        """Single point energy calculation.

        Parameters
        ----------
        mol : System object
            Input system to use in the calculation.
        ncores : int, optional
            number of cores, by default all available cores
        maxcore : dummy variable
            dummy variable used for compatibility with Orca calculations
        charge : int, optional
            total charge of the system. Default is taken from the input system.
        spin : int, optional
            total spin of the system. Default is taken from the input system.
        save_cubes: bool, optional
            if set to True, will save a cube file containing electronic and spin densities,
            by default False.
        cube_step: int, optional
            grid spacing for cube files, in Bohrs (default 0.4)
        inplace : bool, optional
            updates info for the input system instead of outputting a new system object,
            by default False
        remove_tdir : bool, optional
            Temporary work directory will be removed, by default True

        Returns
        -------
        newmol : System object
            Output system containing the new energies.
        """

        if ncores is None:
            ncores = get_ncores()

        if charge is None:
            charge = mol.charge
        if spin is None:
            spin = mol.spin

        logger.info(f"{mol.name}, charge {charge} spin {spin} - {self.method} SPE")
        logger.debug(f"Running xTB calculation on {ncores} cores")

        tdir = mkdtemp(
            prefix=mol.name + "_",
            suffix=f"_{self.method.split()[0]}_spe",
            dir=os.getcwd(),
        )

        with sh.pushd(tdir):

            self.write_input(
                job_info={
                    "charge": charge,
                    "spin": spin,
                    "save_cubes": save_cubes,
                    "cube_step": cube_step,
                },
            )

            mol.geometry.write_xyz(f"{mol.name}.xyz")

            if self.solvent:
                os.system(
                    f"{self.__XTBPATH} --input input.inp {mol.name}.xyz --{self.method} --alpb {self.solvent} -P {ncores} {self.optionals} > output.out 2>> output.err"
                )

            else:
                os.system(
                    f"{self.__XTBPATH} --input input.inp {mol.name}.xyz --{self.method} -P {ncores} {self.optionals} > output.out 2>> output.err"
                )

            with open("output.out", "r") as out:
                for line in out:
                    if "TOTAL ENERGY" in line:
                        electronic_energy = float(line.split()[-3])

            if inplace is False:

                newmol = System(f"{mol.name}.xyz", charge, spin)

                newmol.properties = copy.copy(mol.properties)
                newmol.properties.set_electronic_energy(electronic_energy, self)

            else:
                mol.properties.set_electronic_energy(electronic_energy, self)

            process_output(mol, self.method, "spe", charge, spin)
            if remove_tdir:
                shutil.rmtree(tdir)

        if inplace is False:
            return newmol

    def opt(
        self,
        mol: System,
        ncores: int = None,
        maxcore=None,
        charge: int = None,
        spin: int = None,
        save_cubes: bool = False,
        cube_step: float = 0.1,
        inplace: bool = False,
        remove_tdir: bool = True,
    ):
        """Geometry optimization + frequency analysis.

        Parameters
        ----------
        mol : System object
            Input system to use in the calculation
        ncores : int, optional
            number of cores, by default all available cores
        maxcore : dummy variable
            dummy variable used for compatibility with Orca calculations
        charge : int, optional
            Total charge of the system. Default is taken from the input system.
        spin : int, optional
            Total spin of the system. Default is taken from the input system.
        save_cubes: bool, optional
            if set to True, will save a cube file containing electronic and spin densities,
            by default False.
        cube_step: int, optional
            grid spacing for cube files, in Bohrs (default 0.4)
        inplace : bool, optional
            updates info for the input system instead of outputting a new system object,
            by default False
        remove_tdir : bool, optional
            Temporary work directory will be removed, by default True

        Returns
        -------
        newmol : System object
            Output system containing the new geometry and energies.

        If a dissociation or a cyclization is observed, ignore the calculation and return the
        original "mol" system.
        """

        if ncores is None:
            ncores = get_ncores()

        if charge is None:
            charge = mol.charge
        if spin is None:
            spin = mol.spin

        logger.info(f"{mol.name}, charge {charge} spin {spin} - {self.method} OPT")
        logger.debug(f"Running xTB calculation on {ncores} cores")

        tdir = mkdtemp(
            prefix=mol.name + "_",
            suffix=f"_{self.method.split()[0]}_opt",
            dir=os.getcwd(),
        )

        with sh.pushd(tdir):

            self.write_input(
                job_info={
                    "charge": charge,
                    "spin": spin,
                    "save_cubes": save_cubes,
                    "cube_step": cube_step,
                },
            )

            mol.geometry.write_xyz(f"{mol.name}.xyz")

            if self.solvent:
                os.system(
                    f"{self.__XTBPATH} --input input.inp {mol.name}.xyz --{self.method} --alpb {self.solvent} --ohess -P {ncores} {self.optionals} > output.out 2>> output.err"
                )

            else:
                os.system(
                    f"{self.__XTBPATH} --input input.inp {mol.name}.xyz --{self.method} --ohess -P {ncores} {self.optionals} > output.out 2>> output.err"
                )

            if dissociation_check() is True:
                logger.error(f"Dissociation spotted for {mol.name}.")
                add_flag(
                    mol,
                    f"Dissociation occurred during geometry optimization with {self.method}.",
                )
                return None
            elif cyclization_check(f"{mol.name}.xyz", "xtbopt.xyz") is True:
                logger.error(f"Cyclization change spotted for {mol.name}.")
                add_flag(
                    mol,
                    f"Cyclization change occurred during geometry optimization with {self.method}.",
                )
                return None
            else:
                with open("output.out", "r") as out:
                    for line in out:
                        if "TOTAL ENERGY" in line:
                            electronic_energy = float(line.split()[-3])
                        if "G(RRHO) contrib." in line:
                            vibronic_energy = float(line.split()[-3])

                if inplace is False:

                    newmol = System(f"{mol.name}.xyz", charge, spin)
                    newmol.geometry.load_xyz("xtbopt.xyz")
                    newmol.geometry.level_of_theory_geometry = self.level_of_theory

                    newmol.properties.set_electronic_energy(electronic_energy, self)
                    newmol.properties.set_vibronic_energy(vibronic_energy, self)

                else:
                    mol.geometry.load_xyz("xtbopt.xyz")
                    mol.geometry.level_of_theory_geometry = self.level_of_theory

                    mol.properties.set_electronic_energy(electronic_energy, self)
                    mol.properties.set_vibronic_energy(vibronic_energy, self)

                process_output(mol, self.method, "opt", charge, spin)
                if remove_tdir:
                    shutil.rmtree(tdir)

        if inplace is False:
            return newmol

    def freq(
        self,
        mol: System,
        ncores: int = None,
        maxcore=None,
        charge: int = None,
        spin: int = None,
        save_cubes: bool = False,
        cube_step: float = 0.1,
        inplace: bool = False,
        remove_tdir: bool = True,
    ):
        """Frequency analysis.

        Parameters
        ----------
        mol : System object
            input system to use in the calculation
        ncores : int, optional
            number of cores, by default all available cores
        maxcore : dummy variable
            dummy variable used for compatibility with Orca calculations
        charge : int, optional
            Total charge of the system. Default is taken from the input system.
        spin : int, optional
            Total spin of the system. Default is taken from the input system.
        save_cubes: bool, optional
            if set to True, will save a cube file containing electronic and spin densities,
            by default False.
        cube_step: int, optional
            grid spacing for cube files, in Bohrs (default 0.4)
        inplace : bool, optional
            updates info for the input system instead of outputting a new system object,
            by default False
        remove_tdir : bool, optional
            temporary work directory will be removed, by default True

        Returns
        -------
        newmol : System object
            Output system containing the new energies.
        """

        if ncores is None:
            ncores = get_ncores()

        if charge is None:
            charge = mol.charge
        if spin is None:
            spin = mol.spin

        logger.info(f"{mol.name}, charge {charge} spin {spin} - {self.method} FREQ")
        logger.debug(f"Running xTB calculation on {ncores} cores")

        tdir = mkdtemp(
            prefix=mol.name + "_",
            suffix=f"_{self.method.split()[0]}_freq",
            dir=os.getcwd(),
        )

        with sh.pushd(tdir):

            self.write_input(
                job_info={
                    "charge": charge,
                    "spin": spin,
                    "save_cubes": save_cubes,
                    "cube_step": cube_step,
                },
            )

            mol.geometry.write_xyz(f"{mol.name}.xyz")

            if self.solvent:
                os.system(
                    f"{self.__XTBPATH} --input input.inp {mol.name}.xyz --{self.method} --alpb {self.solvent} --hess -P {ncores} {self.optionals} > output.out 2>> output.err"
                )

            else:
                os.system(
                    f"{self.__XTBPATH} --input input.inp {mol.name}.xyz --{self.method} --hess -P {ncores} {self.optionals} > output.out 2>> output.err"
                )

            with open("output.out", "r") as out:
                for line in out:
                    if "TOTAL ENERGY" in line:
                        electronic_energy = float(line.split()[-3])
                    if "G(RRHO) contrib." in line:
                        vibronic_energy = float(line.split()[-3])

            if inplace is False:

                newmol = System(f"{mol.name}.xyz", charge, spin)

                newmol.properties = copy.copy(mol.properties)

                newmol.properties.set_electronic_energy(electronic_energy, self)
                newmol.properties.set_vibronic_energy(vibronic_energy, self)

            else:

                mol.properties.set_electronic_energy(electronic_energy, self)
                mol.properties.set_vibronic_energy(vibronic_energy, self)

            process_output(mol, self.method, "freq", charge, spin)
            if remove_tdir:
                shutil.rmtree(tdir)

        if inplace is False:
            return newmol
