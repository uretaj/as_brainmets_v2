# amplicon_suite_brainmets

The original code and instructions are from https://github.com/AmpliconSuite/AmpliconSuite-pipeline . It has been modified to generate the seed intervals from the FACETS calls, which are then passed to Amplicon Architect.

## Installation
1.  Obtain the data repository containing  the AmpliconSuite-pipeline image and GRCh37 annotations  :
    * Download the data repo: https://datasets.genepattern.org/?prefix=data/module_support_files/AmpliconArchitect
    * Extract the tar file
         ```bash
         tar zxf   GRCh38.tar.gz.gz
         ```
2. Obtain the execution script
    ```bash
    git clone https://github.com/uretaj/as_brainmets_v2/.git
    ```
3. License for Mosek optimization tool:
    * Obtain license file `mosek.lic` (`https://www.mosek.com/products/academic-licenses/`). The license is free for academic use.
    * Place the file in `$HOME/mosek/` (i.e, the `mosek/` folder that now exists in your home directory).
    * If you are not able to place the license in the default location, you can set a custom location by exporting the bash variable   `MOSEKLM_LICENSE_FILE=/custom/path/`.
    
        ```bash
        export MOSEKLM_LICENSE_FILE="/path/to/mosek.lic"
        ```
An example command might look like:

`as_brainmets_v2/singularity/run_paa_singularity.py -o path/to/output_dir/sample  --bam sample.bam  --scna_file sample.txt --data_repo path/to/data_repo `


Below is a sample Slurm file:
```bash
#!/bin/bash


#SBATCH --job-name=circdna.slurm
#SBATCH --ntasks=1
#SBATCH -t 48:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mail-type=ALL
#SBATCH --output=%x.%a.%j.out # STDOUT 
#SBATCH --error=%x.%a.%j.err  # STDERR
#SBATCH --array=1-40
#SBATCH --mem-per-cpu=100G



module load singularity/3.8.2
export MOSEKLM_LICENSE_FILE="mosek/mosek.lic"
echo "ARRAY ID: ${SLURM_ARRAY_TASK_ID}"
filename=$(head -n ${SLURM_ARRAY_TASK_ID} hlfa_list_all_countries.csv  | tail -1)
filename=${filename%$'\r'}
IFS=',' read -ra arr <<< "$filename"
sample=${arr[0]}
cnv=${arr[1]}
echo "SAMPLE ${sample}"
echo "FILENAME ${cnv}"
pathf="BAM/${sample}.mapped.bam"
cnvpath="Subclonal_SCNA_with_Avg_CN/${cnv}
as_brainmets_v2/singularity/run_paa_singularity.py  -o AA_RESULT/${sample} -t 1 --bam ${pathf}  --scna_file ${cnvpath} --data_repo path/data_repo
```
Here's an example of how to submit a job arrray to run multiple samples (i.e. execute the script for 40 samples but only run 5 samples at a time)

```bash
sbatch --array=1-40%5 amplicon_suite.slurm
```
## Command line arguments to AmpliconSuite-pipeline
#### Required
- `-o  {outdir}`: Directory where results will be stored. Include the sample name to avoid conflicts.
- `--data_repo {repodir} `:  Directory where the singularity image file and  required annotations for GRCh37 are stored.
- `-t ` : Number of threads but it's not really used so just set it to 1.
  
Input files:

  * `--bam {sample.bam}` Coordinate-sorted bam
  * `--scna_file {scna.txt}` Supply the FACETS calls of the sample to generate the seed intervals to be passed to Amplicon Architect. 
