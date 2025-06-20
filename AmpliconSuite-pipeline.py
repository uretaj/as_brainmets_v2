#!/usr/bin/env python

# author: Jens Luebeck (jluebeck [at] ucsd.edu)

import argparse
from datetime import datetime
import json
import logging
import os
import re
import socket
from subprocess import *
import sys
import tarfile
import time

from paalib import check_reference, cnv_prefilter, reduce_fasta
from paalib._version import __ampliconsuitepipeline_version__


PY3_PATH = "python3"  # updated by command-line arg if specified
metadata_dict = {}  # stores the run metadata (bioinformatic metadata)
sample_info_dict = {}  # stores the sample metadata


def run_bwa(ref_fasta, fastqs, outdir, sname, nthreads, samtools, samtools_version):
    outname = outdir + sname
    logging.info("Output prefix: " + outname)
    exts = [".sa", ".amb", ".ann", ".pac", ".bwt"]
    indexPresent = True
    for i in exts:
        if not os.path.exists(ref_fasta + i):
            indexPresent = False
            logging.info("Could not find " + ref_fasta + i + ", building BWA index from scratch. This could take > 60 minutes")
            break

    if not indexPresent:
        cmd = "bwa index " + ref_fasta
        call(cmd, shell=True)

    logging.info("Performing alignment and sorting\n")
    if samtools_version[0] < 1:
        cmd = "{{ bwa mem -K 10000000 -t {} {} {} | {} view -Shu - | {} sort -m 4G -@4 - {}.cs; }} 2>{}_aln_stage.stderr".format(
            nthreads, ref_fasta, fastqs, samtools, samtools, outname, outname)
    else:
        cmd = "{{ bwa mem -K 10000000 -t {} {} {} | {} view -Shu - | {} sort -m 4G -@4 -o {}.cs.bam -; }} 2>{}_aln_stage.stderr".format(
            nthreads, ref_fasta, fastqs, samtools, samtools, outname, outname)

    logging.info(cmd + "\n")
    call(cmd, shell=True)
    metadata_dict["bwa_cmd"] = cmd

    logging.info("Performing duplicate marking & indexing")
    final_bam_name = "{}.cs.rmdup.bam".format(outname)
    cmd_list = [samtools, "rmdup", "-s", "{}.cs.bam".format(outname), final_bam_name]
    logging.info(" ".join(cmd_list) + "\n")
    call(cmd_list)

    logging.info("Running samtools index")
    cmd_list = [samtools, "index", final_bam_name]
    logging.info(" ".join(cmd_list) + "\n")
    call(cmd_list)

    logging.info("Removing temp BAM\n")
    cmd = "rm {}.cs.bam".format(outname)
    call(cmd, shell=True)
    return final_bam_name, outname + "_aln_stage.stderr"


# This is not currently used by AmpliconSuite-pipeline.
def run_freebayes(ref, bam_file, outdir, sname, nthreads, regions, fb_path=None):
    # Freebayes cmd-line args
    # -f is fasta
    # -r is region to call
    logging.info("Running freebayes...")
    fb_exec = "freebayes"
    if fb_path:
        fb_exec = fb_path + "/" + fb_exec
    while True:
        try:
            curr_region_tup = regions.pop()
        except IndexError:
            break

        curr_region_string = curr_region_tup[0] + ":" + curr_region_tup[1]
        logging.info(curr_region_string + ". " + str(len(regions)) + " items remaining.")
        vcf_file = outdir + sname + "_" + curr_region_tup[0] + "_" + curr_region_tup[2] + ".vcf"
        replace_filter_field_func = "awk '{ if (substr($1,1,1) != \"#\" ) { $7 = ($7 == \".\" ? \"PASS\" : $7 ) }} 1 ' OFS=\"\\t\""
        cmd = "{} --genotype-qualities --standard-filters --use-best-n-alleles 5 --limit-coverage 25000 \
        --strict-vcf -f {} -r {} {} | {} > {}".format(fb_exec, ref, curr_region_string, bam_file,
                                                      replace_filter_field_func, vcf_file)
        logging.info(cmd)
        call(cmd, shell=True)
        # gzip the new VCF
        call("gzip -f " + vcf_file, shell=True)


# This is not currently used by AmpliconSuite-pipeline.
def merge_and_filter_vcfs(chr_names, vcf_list, outdir, sname):
    logging.info("Merging VCFs and zipping...\n")
    # collect the vcf files to merge
    merged_vcf_file = outdir + sname + "_merged.vcf"
    relevant_vcfs = [x for x in vcf_list if any([i in x for i in chr_names])]
    chrom_vcf_d = {}
    for f in relevant_vcfs:
        curr_chrom = f.rsplit(".vcf.gz")[0].rsplit("_")[-2:]
        chrom_vcf_d[curr_chrom[0] + curr_chrom[1]] = f

    # chr_nums = [x.lstrip("chr") for x in chr_names]
    pre_chr_str_names = [str(x) for x in range(1, 23)] + ["X", "Y"]

    # sort the elements
    # include the header from the first one
    if args.ref != "GRCh37" and args.ref != "GRCm38":
        sorted_chr_names = ["chr" + str(x) for x in pre_chr_str_names]
        cmd = "zcat " + chrom_vcf_d["chrM"] + ''' | awk '$4 != "N"' > ''' + merged_vcf_file

    else:
        sorted_chr_names = [str(x) for x in pre_chr_str_names]
        cmd = "zcat " + chrom_vcf_d["MT"] + ''' | awk '$4 != "N"' > ''' + merged_vcf_file

    logging.info(cmd + "\n")
    call(cmd, shell=True)

    # zcat the rest, grepping out all header lines starting with "#"
    logging.debug(sorted_chr_names)
    for i in sorted_chr_names:
        if i == "chrM" or i == "MT":
            continue

        cmd_p = "zcat " + chrom_vcf_d[i + "p"] + ''' | grep -v "^#" | awk '$4 != "N"' >> ''' + merged_vcf_file
        cmd_q = "zcat " + chrom_vcf_d[i + "q"] + ''' | grep -v "^#" | awk '$4 != "N"' >> ''' + merged_vcf_file
        logging.info(cmd_p)
        call(cmd_p, shell=True)
        logging.info(cmd_q)
        call(cmd_q, shell=True)

    cmd = "gzip -f " + merged_vcf_file
    logging.info(cmd)
    call(cmd, shell=True)

    return merged_vcf_file + ".gz"


def run_cnvkit(ckpy_path, nthreads, outdir, bamfile, seg_meth='cbs', normal=None, ref_fasta=None, vcf=None):
    # CNVkit cmd-line args
    # -m wgs: wgs data
    # -y: assume chrY present
    # -n: create flat reference (cnv baseline)
    # -p: number of threads
    # -f: reference genome fasta
    bamBase = os.path.splitext(os.path.basename(bamfile))[0]
    cnvkit_version = Popen([PY3_PATH, ckpy_path, "version"], stdout=PIPE, stderr=PIPE).communicate()[0].rstrip()
    try:
        cnvkit_version = cnvkit_version.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["cnvkit_version"] = cnvkit_version

    ckRef = AA_REPO + args.ref + "/" + args.ref + "_cnvkit_filtered_ref.cnn"
    if normal and args.ref == "GRCh38_viral":
        logging.warning("CNVkit does not properly support matched tumor-normal with viral genomes. Ignoring matched-"
                        "normal and running in tumor-only mode.\n")
        
    logging.info("Running CNVKit batch\n")
    if normal and not args.ref == "GRCh38_viral":
        # create a version of the stripped reference
        reduce_fasta.reduce_fasta(ref_fasta, ref_genome_size_file, outdir)
        base = os.path.basename(ref_fasta) # args.ref is the name, ref is the fasta
        stripRefG = outdir + os.path.splitext(base)[0] + "_reduced" + "".join(os.path.splitext(base)[1:])
        logging.debug("Stripped reference: " + stripRefG)
        cmd = "{} {} batch {} -m wgs --fasta {} -p {} -d {} --normal {}".format(PY3_PATH, ckpy_path, bamfile, stripRefG,
                                                                                nthreads, outdir, normal)
    else:
        cmd = "{} {} batch -m wgs -r {} -p {} -d {} {}".format(PY3_PATH, ckpy_path, ckRef, nthreads, outdir, bamfile)

    logging.info(cmd + "\n")
    call(cmd, shell=True)
    metadata_dict["cnvkit_cmd"] = cmd + " ; "
    rscript_str = ""
    if args.rscript_path:
        rscript_str = "--rscript-path " + args.rscript_path
        logging.info("Set Rscript flag: " + rscript_str)

    cnrFile = outdir + bamBase + ".cnr"
    cnsFile = outdir + bamBase + ".cns"
    logging.info("Running CNVKit segment")
    # TODO: possibly include support for adding VCF calls.
    cmd = "{} {} segment {} {} -p {} -m {} -o {}".format(PY3_PATH, ckpy_path, cnrFile, rscript_str, nthreads, seg_meth,
                                                         cnsFile)
    logging.info(cmd + "\n")
    exit_code = call(cmd, shell=True)
    if exit_code != 0:
        logging.error("CNVKit encountered a non-zero exit status. Exiting...\n")
        sys.exit(1)

    metadata_dict["cnvkit_cmd"] = metadata_dict["cnvkit_cmd"] + cmd
    logging.info("Cleaning up temporary CNVkit files")
    cmd = "rm -f {}/*tmp.bed {}/*.cnn {}/*target.bed {}/*.bintest.cns".format(outdir, outdir, outdir, outdir)
    logging.info(cmd)
    call(cmd, shell=True)
    cmd = "gzip -f " + cnrFile
    logging.info(cmd)
    call(cmd, shell=True)
    if normal and not args.ref == "GRCh38_viral":
        cmd = "rm " + stripRefG + " " + stripRefG + ".fai"
        logging.info(cmd)
        call(cmd, shell=True)


# Read the CNVkit .cns files
def convert_cnvkit_cns_to_bed(cnvkit_output_directory, base, cnsfile=None, rescaled=False, nofilter=False):
    if cnsfile is None:
        if not rescaled:
            cnsfile = cnvkit_output_directory + base + ".cns"
        else:
            cnsfile = cnvkit_output_directory + base + "_rescaled.cns"

    with open(cnsfile) as infile, open(cnvkit_output_directory + base + "_CNV_CALLS.bed", 'w') as outfile:
        head = next(infile).rstrip().rsplit("\t")
        for line in infile:
            fields = line.rstrip().rsplit("\t")
            # s, e = int(fields[1]), int(fields[2])
            cn_r = float(fields[4])
            cn = 2 ** (cn_r + 1)
            # do not filter on size since amplified_intervals.py will merge small ones.
            outline = "\t".join(fields[0:3] + ["CNVkit", str(cn)]) + "\n"
            outfile.write(outline)

    return cnvkit_output_directory + base + "_CNV_CALLS.bed"


def rescale_cnvkit_calls(ckpy_path, cnvkit_output_directory, base, cnsfile=None, ploidy=None, purity=None):
    if purity is None and ploidy is None:
        logging.warning("Warning: Rescaling called without --ploidy or --purity. Rescaling will have no effect.")
    if cnsfile is None:
        cnsfile = cnvkit_output_directory + base + ".cns"

    if purity < 0.4:
        logging.warning("WARNING! Rescaling a low purity sample may cause many false-positive seed regions!")
        
    cmd = "{} {} call {} -m clonal".format(PY3_PATH, ckpy_path, cnsfile)
    if purity:
        cmd += " --purity " + str(purity)
    if ploidy:
        cmd += " --ploidy " + str(ploidy)

    cmd += " -o " + cnvkit_output_directory + base + "_rescaled.cns"
    logging.info("Rescaling CNVKit calls\n" + cmd)
    call(cmd, shell=True)


def run_amplified_intervals(AA_interpreter, CNV_seeds_filename, sorted_bam, output_directory, sname, cngain,
                            cnsize_min):
    logging.info("Running amplified_intervals")
    AA_seeds_filename = "{}_AA_CNV_SEEDS".format(output_directory + sname)
    cmd = "{} {}/amplified_intervals.py --ref {} --bed {} --bam {} --gain {} --cnsize_min {} --out {}".format(
        AA_interpreter, AA_SRC, args.ref, CNV_seeds_filename, sorted_bam, str(cngain), str(cnsize_min),
        AA_seeds_filename)

    logging.info(cmd + "\n")
    exit_code = call(cmd, shell=True)
    if exit_code != 0:
        logging.error("amplified_intervals.py returned a non-zero exit code. Exiting...\n")
        sys.exit(1)

    metadata_dict["amplified_intervals_cmd"] = cmd
    return AA_seeds_filename + ".bed"


def run_AA(amplified_interval_bed, AA_outdir, sname, args):
    AA_interpreter = args.aa_python_interpreter
    sorted_bam = args.bam
    downsample = args.downsample
    ref = args.ref
    runmode = args.AA_runmode
    extendmode = args.AA_extendmode
    insert_sdevs = args.AA_insert_sdevs
    sv_vcf = args.sv_vcf
    sv_vcf_no_filter = args.sv_vcf_no_filter

    AA_version = \
    Popen([AA_interpreter, AA_SRC + "/AmpliconArchitect.py", "--version"], stdout=PIPE, stderr=PIPE).communicate()[
        0].rstrip()
    try:
        AA_version = AA_version.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["AA_version"] = AA_version

    cmd = "{} {}/AmpliconArchitect.py --ref {} --downsample {} --bed {} --bam {} --runmode {} --extendmode {} --out {}/{}".format(
        AA_interpreter, AA_SRC, ref, str(downsample), amplified_interval_bed, sorted_bam, runmode, extendmode,
        AA_outdir, sname)
    if insert_sdevs is not None:
        cmd += " --insert_sdevs {}".format(str(insert_sdevs))

    if sv_vcf:
        cmd += " --sv_vcf {}".format(sv_vcf)
        if sv_vcf_no_filter:
            cmd += " --sv_vcf_no_filter"

    logging.info(cmd + "\n")
    aa_exit_code = call(cmd, shell=True)
    if aa_exit_code != 0:
        logging.error("AmpliconArchitect returned a non-zero exit code. Exiting...\n")
        sys.exit(1)

    metadata_dict["AA_cmd"] = cmd


def run_AC(AA_outdir, sname, ref, AC_outdir, AC_src):
    logging.info("Running AC")
    # make input file
    class_output = AC_outdir + sname
    input_file = class_output + ".input"
    bed_dir = class_output + "_classification_bed_files/"
    if os.path.exists(bed_dir):
        logging.warning("WARNING! AC files were not cleared prior to re-running. New classifications may become "
                        "mixed with previous classification files!")

    cmd = "{}/make_input.sh {} {}".format(AC_src, AA_outdir, class_output)
    logging.info(cmd)
    call(cmd, shell=True)

    with open(input_file) as ifile:
        sample_info_dict["number_of_AA_amplicons"] = len(ifile.readlines())

    cmd = "{} {}/amplicon_classifier.py -i {} --ref {} -o {} --report_complexity".format(PY3_PATH, AC_src, input_file,
                                                                                         ref, class_output)
    logging.info(cmd + "\n")
    call(cmd, shell=True)
    metadata_dict["AC_cmd"] = cmd

    # Get AC version
    AC_version = \
    Popen([PY3_PATH, AC_src + "/amplicon_classifier.py", "--version"], stdout=PIPE, stderr=PIPE).communicate()[
        0].rstrip()
    try:
        AC_version = AC_version.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["AC_version"] = AC_version

    # iterate over the bed files and count anything that isn't "unknown" as a feature
    feat_count = 0
    if os.path.exists(bed_dir):
        for bf in os.listdir(bed_dir):
            if not "unknown" in bf and bf.endswith(".bed"):
                feat_count += 1

    sample_info_dict["number_of_AA_features"] = feat_count


def make_AC_table(sname, AC_outdir, AC_src, run_metadata_file, sample_metadata_file, ref, cnv_bed=None):
    # make the AC output table
    class_output = AC_outdir + sname
    input_file = class_output + ".input"
    summary_map_file = class_output + "_summary_map.txt"
    classification_file = class_output + "_amplicon_classification_profiles.tsv"
    cmd = "{} {}/make_results_table.py -i {} --classification_file {} --summary_map {} --ref {}".format(
        PY3_PATH, AC_src, input_file, classification_file, summary_map_file, ref)

    if cnv_bed:
        cmd += " --cnv_bed " + cnv_bed

    if run_metadata_file:
        cmd += " --run_metadata_file " + run_metadata_file

    if sample_metadata_file:
        cmd += " --sample_metadata_file " + sample_metadata_file

    logging.info(cmd + "\n")
    call(cmd, shell=True)


def get_ref_sizes(ref_genome_size_file):
    chr_sizes = {}
    with open(ref_genome_size_file) as infile:
        for line in infile:
            fields = line.rstrip().rsplit()
            if fields:
                chr_sizes[fields[0]] = str(int(fields[1]) - 1)

    return chr_sizes


def get_ref_centromeres(ref_name):
    centromere_dict = {}
    fnameD = {"GRCh38": "GRCh38_centromere.bed", "GRCh37": "human_g1k_v37_centromere.bed",
              "hg19": "hg19_centromere.bed",
              "mm10": "mm10_centromere.bed", "GRCm38": "GRCm38_centromere.bed", "GRCh38_viral": "GRCh38_centromere.bed"}
    with open(AA_REPO + ref_name + "/" + fnameD[ref_name]) as infile:
        for line in infile:
            if not "centromere" in line and not "acen" in line:
                continue
            fields = line.rstrip().rsplit("\t")
            if fields[0] not in centromere_dict:
                centromere_dict[fields[0]] = (fields[1], fields[2])

            else:
                pmin = min(int(centromere_dict[fields[0]][0]), int(fields[1]))
                pmax = max(int(centromere_dict[fields[0]][1]), int(fields[2]))
                # pad with 20kb to avoid freebayes issues in calling near centromeres
                centromere_dict[fields[0]] = (str(pmin - 20000), str(pmax + 20000))

    return centromere_dict


def save_run_metadata(outdir, sname, args, launchtime, commandstring):
    # make a dictionary that stores
    # datetime
    # hostname
    # ref
    # PAA command
    # AA python interpreter version
    # bwa cmd
    # CN cmd
    # AA cmd
    # PAA version
    # CNVKit version
    # AA version
    # AC version
    metadata_dict["launch_datetime"] = launchtime
    metadata_dict["hostname"] = socket.gethostname()
    metadata_dict["ref_genome"] = args.ref
    aapint = args.aa_python_interpreter if args.aa_python_interpreter else "python"
    aa_python_v = Popen([aapint, "--version"], stdout=PIPE, stderr=PIPE).communicate()[0].rstrip()
    try:
        aa_python_v = aa_python_v.decode('utf-8')
    except UnicodeError:
        pass

    metadata_dict["AA_python_version"] = aa_python_v
    metadata_dict["AmpliconSuite-pipeline_command"] = commandstring
    metadata_dict["AmpliconSuite-pipeline_version"] = __ampliconsuitepipeline_version__
    metadata_dict["Samtools version"] = "{}.{}".format(samtools_version[0], samtools_version[1])

    for x in ["bwa_cmd", "cnvkit_cmd", "amplified_intervals_cmd", "AA_cmd", "AC_cmd", "cnvkit_version", "AA_version",
              "AC_version"]:
        if x not in metadata_dict:
            metadata_dict[x] = "NA"

    # save the json dict
    run_metadata_filename = outdir + sname + "_run_metadata.json"
    with open(run_metadata_filename, 'w') as fp:
        json.dump(metadata_dict, fp, indent=2)

    # sample_info_dict["run_metadata_file"] = run_metadata_filename
    return run_metadata_filename


def detect_run_failure(align_stderr_file, AA_outdir, sname, AC_outdir):
    if align_stderr_file:
        cmd = 'grep -i error ' + align_stderr_file
        try:
            aln_errs = check_output(cmd, shell=True).decode("utf-8")

        except CalledProcessError:
            aln_errs = ""

        if aln_errs:
            logging.error("Detected error during bwa mem alignment stage\n")
            return True

    if AA_outdir:
        sumfile = AA_outdir + sname + "_summary.txt"
        if os.path.isfile(sumfile):
            namps = -1
            with open(sumfile) as infile:
                for line in infile:
                    if line.startswith("#Amplicons = "):
                        namps = int(line.rstrip().rsplit(" = ")[-1])
                        break

            if namps < 0:
                logging.error("Detected truncated or missing AA outputs")
                return True

            for x in range(1, namps + 1):
                try:
                    fsize = os.stat(AA_outdir + sname + "_amplicon" + str(x) + "_cycles.txt").st_size

                except OSError:
                    fsize = 0

                if fsize == 0:
                    logging.error("Detected truncated or missing AA outputs")
                    return True

        else:
            logging.error("Detected error during AA stage")
            return True

    if AC_outdir:
        try:
            fsize1 = os.stat(AC_outdir + sname + "_amplicon_classification_profiles.tsv").st_size
            fsize2 = os.stat(AC_outdir + sname + "_result_table.tsv").st_size

        except OSError:
            fsize1 = 0
            fsize2 = 0

        if fsize1 == 0 or fsize2 == 0:
            logging.error("Detected error during AC stage\n")
            return True

    return False


def get_samtools_version(samtools):
    try:
        # Run the command to get the version information
        result = Popen([samtools], stderr=PIPE, stdout=PIPE)
        _, output = result.communicate()

        # Decode the output if it's in bytes (Python 3)
        if isinstance(output, bytes):
            output = output.decode('utf-8')

        # Parse the version information to extract major and minor versions
        version_pattern = r'Version: (\d+)\.(\d+)'
        match = re.search(version_pattern, output)
        if match:
            major_version = int(match.group(1))
            minor_version = int(match.group(2))
            return major_version, minor_version
        else:
            # Return None if version information couldn't be parsed
            return None, None
    except OSError as e:
        # Handle the case when Samtools is not found
        logging.error("Error: Samtools not found. Please make sure it is installed and in your PATH.")
        return None, None


def download_file(url, destination_folder):
    import urllib.request  # here because python2 not work with it
    filename = os.path.join(destination_folder, url.split("/")[-1])
    try:
        response = urllib.request.urlopen(url)
        file_size = int(response.headers.get('Content-Length', 0))
        response.close()
        file_size = round(file_size / (1024**3), 2)
        if file_size > 0.1:
            print("\nDownloading " + url + " ... (" + str(file_size) + "GB)")
        else:
            print("\nDownloading " + url + " ...")

        urllib.request.urlretrieve(url, filename)
        print("File downloaded and saved to: " + str(filename))
    except Exception as e:
        print("Failed to download file. Error: " + str(e))


def extract_tar_gz(file_path, destination_folder):
    if not file_path.endswith('.tar.gz'):
        sys.stderr.write("Cannot extract file " + file_path)
        sys.exit(1)

    with tarfile.open(file_path, 'r:gz') as tar:
        tar.extractall(destination_folder)

    os.remove(file_path)


def contains_spaces(file_path):
    return any(char == ' ' for char in file_path)


# MAIN #
if __name__ == '__main__':
    # Parses the command line arguments
    parser = argparse.ArgumentParser(
        description="A pipeline wrapper for AmpliconArchitect, invoking alignment CNV calling and CNV filtering prior. "
                    "Can launch AA, as well as downstream amplicon classification.")
    parser.add_argument("-v", "--version", action='version',
                        version='AmpliconSuite-pipeline version {version} \n'.format(version=__ampliconsuitepipeline_version__))
    parser.add_argument("--download_repo", help="Download the selected data repo to the $AA_DATA_REPO "
                        "directory and exit. '_indexed' suffix indicates BWA index is included, which is useful if "
                        "performing alignment with AmpliconSuite-pipeline, but has a larger filesize.", choices=["hg19",
                        "GRCh37", "GRCh38", "mm10", "GRCh38_viral", "hg19_indexed", "GRCh37_indexed", "GRCh38_indexed",
                        "mm10_indexed", "GRCh38_viral_indexed"], nargs='+')
    parser.add_argument("-o", "--output_directory", metavar='PATH', help="output directory names (will create if not already created)")
    parser.add_argument("-s", "--sample_name", metavar='STR', help="(Required) Sample name")
    parser.add_argument("-t", "--nthreads", metavar='INT', help="(Required) Number of threads to use in BWA and CNV calling")
    parser.add_argument("--run_AA", help="Run AA after all files prepared. Default off.", action='store_true')
    parser.add_argument("--run_AC", help="Run AmpliconClassifier after all files prepared. Default off.",
                        action='store_true')
    parser.add_argument("--ref", metavar='STR', help="Reference genome version.", choices=["hg19", "GRCh37",
                        "GRCh38", "hg38", "mm10", "GRCm38", "GRCh38_viral"])
    parser.add_argument("--cngain", metavar='FLOAT', type=float, help="CN gain threshold to consider for AA seeding",
                        default=4.5)
    parser.add_argument("--cnsize_min", metavar='INT', type=int, help="CN interval size (in bp) to consider for AA seeding",
                        default=50000)
    parser.add_argument("--downsample", metavar='FLOAT', type=float, help="AA downsample argument (see AA documentation)",
                        default=10)
    parser.add_argument("--rscript_path", metavar='PATH', help="Specify custom path to Rscript for CNVKit, "
                        "which requires R version >=3.5")
    parser.add_argument("--python3_path", metavar='PATH', help="If needed, specify a custom path to python3.")
    parser.add_argument("--aa_python_interpreter",
                        help="By default AmpliconSuite-pipeline will use the system's default python path. If you would like to use "
                             "a different python version with AA, set this to either the path to the interpreter or "
                             "'python3' or 'python2' (default 'python')", metavar='PATH', type=str, default='python')
    parser.add_argument("--sv_vcf",
                        help="Provide a VCF file of externally-called SVs to augment SVs identified by AA internally.",
                        metavar='FILE', action='store', type=str)
    parser.add_argument("--sv_vcf_no_filter", help="Use all external SV calls from the --sv_vcf arg, even "
                        "those without 'PASS' in the FILTER column.", action='store_true', default=False)
    parser.add_argument("--AA_src", metavar='PATH', help="Specify a custom $AA_SRC path. Overrides the bash variable")
    parser.add_argument("--AA_runmode", metavar='STR', help="If --run_AA selected, set the --runmode argument to AA. Default mode is "
                        "'FULL'", choices=['FULL', 'BPGRAPH', 'CYCLES', 'SVVIEW'], default='FULL')
    parser.add_argument("--AA_extendmode", metavar='STR', help="If --run_AA selected, set the --extendmode argument to AA. Default "
                        "mode is 'EXPLORE'", choices=["EXPLORE", "CLUSTERED", "UNCLUSTERED", "VIRAL"], default='EXPLORE')
    parser.add_argument("--AA_insert_sdevs", help="Number of standard deviations around the insert size. May need to "
                        "increase for sequencing runs with high variance after insert size selection step. (default "
                        "3.0)", metavar="FLOAT", type=float, default=None)
    parser.add_argument("--normal_bam", metavar='FILE', help="Path to matched normal bam for CNVKit (optional)")
    parser.add_argument("--ploidy", metavar='FLOAT', type=float, help="Ploidy estimate for CNVKit (optional). This is not used outside of CNVKit.",
                        default=None)
    parser.add_argument("--purity", metavar='FLOAT', type=float, help="Tumor purity estimate for CNVKit (optional). This is not used outside of CNVKit.",
                        default=None)
    parser.add_argument("--cnvkit_segmentation", metavar='STR', help="Segmentation method for CNVKit (if used), defaults to CNVKit "
                        "default segmentation method (cbs).", choices=['cbs', 'haar', 'hmm', 'hmm-tumor', 'hmm-germline', 'none'],
                        default='cbs')
    parser.add_argument("--no_filter", help="Do not run amplified_intervals.py to remove low confidence candidate seed"
                                            " regions overlapping repetitive parts of the genome", action='store_true')
    parser.add_argument("--no_QC", help="Skip QC on the BAM file. Do not adjust AA insert_sdevs for "
                                        "poor-quality insert size distribution", action='store_true')
    parser.add_argument("--sample_metadata", metavar='FILE', help="JSON file of sample metadata to build on")
    parser.add_argument("--samtools_path", help="Path to samtools binary (e.g., /path/to/my/samtools). If unset, will use samtools on system path.",
                        default='')
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bam", "--sorted_bam", metavar='FILE', help="Coordinate sorted BAM file (aligned to an AA-supported "
                                                     "reference.)")
    group.add_argument("--fastqs", metavar='TWO FILES', help="Fastq files (r1.fq r2.fq)", nargs=2)
    group.add_argument("--completed_AA_runs", metavar='PATH',
                       help="Path to a directory containing one or more completed AA runs which utilized the same reference genome.")
    group2 = parser.add_mutually_exclusive_group()
    group2.add_argument("--cnv_bed", "--bed", metavar='FILE',
                        help="BED file (or CNVKit .cns file) of CNV changes. Fields in the bed file should"
                             " be: chr start end name cngain")
    group2.add_argument("--cnvkit_dir", metavar='PATH', help="Path to cnvkit.py. Assumes CNVKit is on the system path if not set",
                        default="")
    group2.add_argument("--completed_run_metadata", metavar='FILE',
                        help="Run metadata JSON to retroactively assign to collection of samples", default="")
    group2.add_argument("--align_only", help="Only perform the alignment stage (do not run CNV calling and seeding",
                        action='store_true')

    # start timing
    ta = time.time()
    ti = ta
    launchtime = str(datetime.now())
    args = parser.parse_args()

    # Check if AA_REPO set, print error and quit if not
    try:
        AA_REPO = os.environ['AA_DATA_REPO'] + "/"

    except KeyError:
        sys.stderr.write("AA_DATA_REPO bash variable not found. Please see installation instructions and run ./install.sh before using.\n")
        sys.exit(1)

    # Download any requested data repo files
    if args.download_repo:
        # launch data repo download and exit
        data_repo_base_url = "https://datasets.genepattern.org/data/module_support_files/AmpliconArchitect/"
        for ref in args.download_repo:
            print(ref)
            ref_base_url = data_repo_base_url + ref
            md5file = ref_base_url + "_md5sum.txt"
            ref_file = ref_base_url + ".tar.gz"
            if os.path.exists(AA_REPO + ref):
                print("An AA data repo directory already exists for " + ref + " and it will be replaced!")
            download_file(md5file, AA_REPO)
            download_file(ref_file, AA_REPO)
            print("Extracting...\n")
            extract_tar_gz(AA_REPO + ref + ".tar.gz", AA_REPO)

        print("Finished")
        sys.exit(0)

    # Preflight checks for running AS-pipeline
    if not args.sample_name:
        parser.error("--sample_name (-s) is a required argument.")
    if not args.nthreads:
        parser.error("--nthreads (-t) is a required argument.")
    if not any([args.bam, args.fastqs, args.completed_AA_runs]):
        parser.error("One of --bam | --fastqs | --completed_AA_runs is required!")

    # set an output directory if user did not specify
    if not args.output_directory:
        args.output_directory = os.getcwd()

    if not args.output_directory.endswith("/"):
        args.output_directory += "/"

    sname = args.sample_name
    outdir = args.output_directory
    sample_metadata_filename = args.output_directory + sname + "_sample_metadata.json"
    
    # set samtools version for use
    if not args.samtools_path.endswith("/samtools"):
        if args.samtools_path and not args.samtools_path.endswith("/"):
            args.samtools_path += "/"
        args.samtools_path += "samtools"

    # Make and clear necessary directories.
    # make the output directory location if it does not exist
    if not os.path.exists(args.output_directory):
        os.mkdir(args.output_directory)

    # initiate logging
    paa_logfile = args.output_directory + sname + '.log'
    logging.basicConfig(filename=paa_logfile, format='[%(name)s:%(levelname)s]\t%(message)s',
                        level=logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(name)s:%(levelname)s]\t%(message)s')
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)
    logging.info("Launched on " + launchtime)
    logging.info("AmpiconSuite-pipeline version " + __ampliconsuitepipeline_version__ + "\n")

    commandstring = ""
    for arg in sys.argv:
        if ' ' in arg:
            commandstring += '"{}" '.format(arg)
        else:
            commandstring += "{} ".format(arg)

    logging.info("AmpliconSuite-pipeline command:")
    logging.info(commandstring + "\n")

    if "/" in args.sample_name:
        logging.error("Sample name -s cannot be a path. Specify output directory with -o.\n")
        sys.exit(1)

    finish_flag_filename = args.output_directory + args.sample_name + "_finish_flag.txt"
    if os.path.exists(finish_flag_filename):
        logging.warning("WARNING: Running AmpliconSuite-pipeline.py with outputs directed into the same output location"
                        " as a previous run may cause crashes or other unexpected behavior. To avoid errors, clear "
                        "previous files before re-running.\n")

    with open(finish_flag_filename, 'w') as ffof:
        ffof.write("UNSUCCESSFUL\n")

    timing_logfile = open(args.output_directory + args.sample_name + '_timing_log.txt', 'w')
    timing_logfile.write("#stage:\twalltime(seconds)\n")

    samtools_version = get_samtools_version(args.samtools_path)
    if samtools_version:
        logging.debug("Samtools version: {}.{}".format(samtools_version[0], samtools_version[1]))
    else:
        logging.error("Failed to retrieve Samtools version.")
        sys.exit(1)

    # Check if expected system paths and files are present. Check if provided argument combinations are valid.
    if args.AA_src:
        os.environ['AA_SRC'] = args.AA_src

    if not os.path.exists(os.path.join(AA_REPO, "coverage.stats")):
        logging.info("coverage.stats file not found in " + AA_REPO + "\nCreating a new coverage.stats file.")
        cmd = "touch {}coverage.stats && chmod a+rw {}coverage.stats".format(AA_REPO, AA_REPO)
        logging.info(cmd)
        call(cmd, shell=True)

    try:
        AA_SRC = os.environ['AA_SRC']

    except KeyError:
        try:
            import ampliconarchitectlib
            AA_SRC = os.path.realpath(os.path.dirname(ampliconarchitectlib.__file__))

        except ModuleNotFoundError:
            logging.error("AA_SRC bash variable or library files not found. AmpliconArchitect may not be properly installed.\n")
            sys.exit(1)

    try:
        AC_SRC = os.environ['AC_SRC']

    except KeyError:
        try:
            import ampclasslib
            ac_path = check_output("which amplicon_classifier.py", shell=True).decode("utf-8")
            AC_SRC = ac_path.rsplit("/amplicon_classifier.py")[0]

        except Exception as e:
            logging.error(e)
            logging.error(
                "\nAC_SRC bash variable or library files not found. AmpliconClassifier may not be properly installed.\n")
            sys.exit(1)

    if (args.fastqs or args.completed_AA_runs) and not args.ref:
        logging.error("Must specify --ref when providing unaligned fastq files or completed AA runs.\n")
        sys.exit(1)

    if args.completed_run_metadata.lower() == "none":
        args.completed_run_metadata = None

    # if not these args are set, assume cnvkit.py is on the path.
    if not (args.cnv_bed or args.cnvkit_dir or args.completed_run_metadata or args.align_only) and (args.fastqs or
                                                                                                    args.bam):
        try:
            args.cnvkit_dir = str(check_output(["which cnvkit.py"], shell=True).decode("utf-8").rstrip())

        except CalledProcessError:
            logging.error("cnvkit.py not found on system path. Must specify --cnvkit_dir")
            sys.exit(1)

    elif args.cnvkit_dir and not args.cnvkit_dir.endswith("/") and not args.cnvkit_dir.endswith("cnvkit.py"):
        args.cnvkit_dir += "/"

    else:
        args.completed_run_metadata = None

    if not args.cnvkit_dir.endswith("cnvkit.py"):
        args.cnvkit_dir += "cnvkit.py"

    if args.run_AA:
        if not os.path.exists(os.environ["HOME"] + "/mosek/mosek.lic") and not "MOSEKLM_LICENSE_FILE" in os.environ:
            logging.error("--run_AA set, but MOSEK license not found in $HOME/mosek/")
            sys.exit(1)

        elif "MOSEKLM_LICENSE_FILE" in os.environ:
            if os.environ["MOSEKLM_LICENSE_FILE"].endswith("mosek.lic"):
                logging.error("MOSEKLM_LICENSE_FILE should be the path of the directory of the license, not the full path. Please update your .bashrc, and run 'source ~/.bashrc'")
                sys.exit(1)

            elif not os.path.exists(os.environ["MOSEKLM_LICENSE_FILE"] + "/mosek.lic"):
                logging.error("--run_AA set, but MOSEK license not found in " + os.environ["MOSEKLM_LICENSE_FILE"])
                sys.exit(1)

    runCNV = None
    if args.cnvkit_dir and not args.cnv_bed:
        runCNV = "CNVkit"
        # check Rscript version
        test_rscript = "Rscript"
        if args.rscript_path:
            if not args.rscript_path.endswith("/Rscript"):
                args.rscript_path += "/Rscript"

            test_rscript = args.rscript_path

        try:
            rscript_version_out = str(check_output([test_rscript, "--version"], stderr=STDOUT).decode("utf-8").rstrip())

        except CalledProcessError:
            logging.error(test_rscript + " not found. Must specify --rscript_path")
            sys.exit(1)

    if args.python3_path:
        if not args.python3_path.endswith("/python") and not args.python3_path.endswith("/python3"):
            args.python3_path += "/python3"

        PY3_PATH = args.python3_path

    if args.aa_python_interpreter and not any(args.aa_python_interpreter.endswith(x) for x in ['python', 'python2', 'python3']):
        logging.error("--aa_python_interpreter must be a path of a valid python interpreter")
        sys.exit(1)

    refFnames = {x: None for x in ["hg19", "GRCh37", "GRCh38", "GRCh38_viral", "mm10"]}
    # Paths of all the repo files needed
    if args.ref == "hg38":
        args.ref = "GRCh38"
    if args.ref == "GRCm38":
        args.ref = "mm10"

    for rname in refFnames.keys():
        if os.path.exists(AA_REPO + "/" + rname):
            refFnames[rname] = check_reference.get_ref_fname(AA_REPO, rname)

    faidict = {}
    if args.bam:
        if contains_spaces(args.bam):
            logging.error("BAM filepath cannot contain spaces!")
            sys.exit(1)

        if args.ref and refFnames[args.ref]:
            faidict[args.ref] = AA_REPO + args.ref + "/" + refFnames[args.ref] + ".fai"

        elif args.ref and refFnames[args.ref] is None:
            em = "Data repo files for ref " + args.ref + " not found. Please download using the '--download_repo " + args.ref + "' option\n"
            logging.error(em)
            sys.exit(1)

        else:
            for k, v in refFnames.items():
                if v:
                    faidict[k] = AA_REPO + k + "/" + v + ".fai"

        determined_ref = check_reference.check_ref(args.bam, faidict, args.samtools_path)
        if not determined_ref and not args.ref:
            logging.error("Could not determine ref build. Please make sure AA data repo is populated.")
            sys.exit(1)

        elif not args.ref:
            args.ref = determined_ref

        elif args.ref and not determined_ref:
            logging.warning("WARNING! The BAM file did not match " + args.ref)

    try:
        with open(AA_REPO + args.ref + "/last_updated.txt", 'r') as file:
            datestring = file.read()
            logging.info(args.ref + " data repo constructed on " + datestring)

    except FileNotFoundError:
        logging.warning("Data repo appears to be out of date. Please update your data repo!\n")

    gdir = AA_REPO + args.ref + "/"
    ref_fasta = gdir + refFnames[args.ref]
    ref_genome_size_file = gdir + args.ref + "_noAlt.fa.fai"
    if args.cnv_bed and not os.path.isfile(args.cnv_bed):
        logging.error("Specified CNV bed file does not exist: " + args.cnv_bed + "\n")
        sys.exit(1)

    if not args.sample_metadata:
        args.sample_metadata = os.path.realpath(os.path.dirname(check_reference.__file__)) + "/sample_metadata_skeleton.json"

    with open(args.sample_metadata) as input_json:
        sample_info_dict = json.load(input_json)

    sample_info_dict["reference_genome"] = args.ref
    sample_info_dict["sample_name"] = sname

    tb = time.time()
    timing_logfile.write("Initialization:\t" + "{:.2f}".format(tb - ta) + "\n")
    ta = tb
    logging.info("Running AmpliconSuite-pipeline on sample: " + sname)
    # Begin pipeline
    aln_stage_stderr = None
    if args.fastqs:
        # Run BWA
        if args.fastqs[0] == args.fastqs[1]:
            logging.error(str(args.fastqs))
            logging.error("You must provide two different fastq files for paired-end reads!\n")
            sys.exit(1)

        elif contains_spaces(args.fastqs[0]) or contains_spaces(args.fastqs[1]):
            logging.error("FASTQ filepaths cannot contain spaces!")
            sys.exit(1)

        fastqs = " ".join(args.fastqs)
        logging.info("Will perform alignment on " + fastqs)
        args.bam, aln_stage_stderr = run_bwa(ref_fasta, fastqs, outdir, sname, args.nthreads, args.samtools_path, samtools_version)

    if not args.completed_AA_runs:
        bamBaiNoExt = args.bam[:-3] + "bai"
        cramCraiNoExt = args.bam[:-4] + "crai"
        baiExists = os.path.isfile(args.bam + ".bai") or os.path.isfile(bamBaiNoExt)
        craiExists = os.path.isfile(args.bam + ".crai") or os.path.isfile(cramCraiNoExt)
        if not baiExists and not craiExists:
            logging.info(args.bam + " index not found, calling samtools index")
            call([args.samtools_path, "index", args.bam])
            logging.info("Finished indexing")

        bambase = os.path.splitext(os.path.basename(args.bam))[0]
        prop_paired_proportion = None
        if not args.no_QC:
            logging.debug("samtools path is set to: " + args.samtools_path)
            prop_paired_proportion = check_reference.check_properly_paired(args.bam, args.samtools_path)

        tb = time.time()
        timing_logfile.write("Alignment, indexing and QC:\t" + "{:.2f}".format(tb - ta) + "\n")

        if args.align_only:
            logging.info("Completed\n")
            tf = time.time()
            timing_logfile.write("Total_elapsed_walltime\t" + "{:.2f}".format(tf - ti) + "\n")
            timing_logfile.close()
            sys.exit()

        ta = tb
        centromere_dict = get_ref_centromeres(args.ref)
        chr_sizes = get_ref_sizes(ref_genome_size_file)
        # coordinate CNV calling
        cnvkit_output_directory = None
        if runCNV == "CNVkit":
            cnvkit_output_directory = args.output_directory + sname + "_cnvkit_output/"
            if not os.path.exists(cnvkit_output_directory):
                os.mkdir(cnvkit_output_directory)

            run_cnvkit(args.cnvkit_dir, args.nthreads, cnvkit_output_directory, args.bam,
                       seg_meth=args.cnvkit_segmentation, normal=args.normal_bam, ref_fasta=ref_fasta)
            if args.ploidy or args.purity:
                rescale_cnvkit_calls(args.cnvkit_dir, cnvkit_output_directory, bambase, ploidy=args.ploidy,
                                     purity=args.purity)
                rescaling = True
            else:
                rescaling = False

            args.cnv_bed = convert_cnvkit_cns_to_bed(cnvkit_output_directory, bambase, rescaled=rescaling)

        if args.cnv_bed.endswith(".cns"):
            args.cnv_bed = convert_cnvkit_cns_to_bed(outdir, bambase, cnsfile=args.cnv_bed, nofilter=True)

        tb = time.time()
        timing_logfile.write("CNV calling:\t" + "{:.2f}".format(tb - ta) + "\n")
        ta = tb

        sample_info_dict["sample_cnv_bed"] = args.cnv_bed

        if not args.no_filter and not args.cnv_bed.endswith("_AA_CNV_SEEDS.bed"):
            if not args.cnv_bed.endswith("_CNV_CALLS_pre_filtered.bed") and not args.cnv_bed.endswith("_CNV_CALLS_unfiltered_gains.bed"):
                pfilt_odir = cnvkit_output_directory if cnvkit_output_directory else args.output_directory
                args.cnv_bed = cnv_prefilter.prefilter_bed(args.cnv_bed, args.ref, centromere_dict, chr_sizes,
                                                           args.cngain, pfilt_odir)

            amplified_interval_bed = run_amplified_intervals(args.aa_python_interpreter, args.cnv_bed, args.bam,
                                                             outdir, sname, args.cngain, args.cnsize_min)

        elif args.no_filter and runCNV:
            if not args.cnv_bed.endswith("_CNV_CALLS_pre_filtered.bed") and not args.cnv_bed.endswith("_CNV_CALLS_unfiltered_gains.bed"):
                pfilt_odir = cnvkit_output_directory if cnvkit_output_directory else args.output_directory
                args.cnv_bed = cnv_prefilter.prefilter_bed(args.cnv_bed, args.ref, centromere_dict, chr_sizes,
                                                           args.cngain, pfilt_odir)
                logging.info("Skipping amplified_intervals.py step due to --no_filter")

        else:
            logging.info("Skipping filtering of bed file.")
            amplified_interval_bed = args.cnv_bed

        tb = time.time()
        timing_logfile.write("Seed filtering (amplified_intervals.py):\t" + "{:.2f}".format(tb - ta) + "\n")
        ta = tb

        # Run AA
        if args.run_AA:
            AA_outdir = outdir + sname + "_AA_results/"
            if not os.path.exists(AA_outdir):
                os.mkdir(AA_outdir)

            # set the insert sdevs if not given by user.
            if not args.no_QC and not args.AA_insert_sdevs and prop_paired_proportion is not None and prop_paired_proportion < 90:
                logging.info("Properly paired rate less than 90%, setting --insert_sdevs 9.0 for AA")
                args.AA_insert_sdevs = 9.0

            run_AA(amplified_interval_bed, AA_outdir, sname, args)
            tb = time.time()
            timing_logfile.write("AmpliconArchitect:\t" + "{:.2f}".format(tb - ta) + "\n")
            ta = tb
            # Run AC
            if args.run_AC:
                AC_outdir = outdir + sname + "_classification/"
                if not os.path.exists(AC_outdir):
                    os.mkdir(AC_outdir)

                run_AC(AA_outdir, sname, args.ref, AC_outdir, AC_SRC)

                tb = time.time()
                timing_logfile.write("AmpliconClassifier:\t" + "{:.2f}".format(tb - ta) + "\n")

        run_metadata_filename = save_run_metadata(outdir, sname, args, launchtime, commandstring)

        with open(sample_metadata_filename, 'w') as fp:
            json.dump(sample_info_dict, fp, indent=2)

        if args.run_AA and args.run_AC:
            make_AC_table(sname, AC_outdir, AC_SRC, run_metadata_filename, sample_metadata_filename,
                          args.ref, cnv_bed=sample_info_dict["sample_cnv_bed"])

    else:
        ta = time.time()
        if not args.ref:
            logging.error("--ref is a required argument if --completed_AA_runs is provided!")
            sys.exit(1)

        AC_outdir = outdir + sname + "_classification/"
        if not os.path.exists(AC_outdir):
            os.mkdir(AC_outdir)

        run_AC(args.completed_AA_runs, sname, args.ref, AC_outdir, AC_SRC)

        tb = time.time()
        timing_logfile.write("AmpliconClassifier:\t" + "{:.2f}".format(tb - ta) + "\n")

        with open(sample_metadata_filename, 'w') as fp:
            json.dump(sample_info_dict, fp, indent=2)

        make_AC_table(sname, AC_outdir, AC_SRC, args.completed_run_metadata, sample_metadata_filename, args.ref)

    if not args.run_AA:
        AA_outdir = None

    if not args.run_AC:
        AC_outdir = None

    if not detect_run_failure(aln_stage_stderr, AA_outdir, sname, AC_outdir):
        logging.info("All stages appear to have completed successfully.")
        with open(args.output_directory + args.sample_name + "_finish_flag.txt", 'w') as ffof:
            ffof.write("All stages completed\n")


    tf = time.time()
    timing_logfile.write("Total_elapsed_walltime\t" + "{:.2f}".format(tf - ti) + "\n")
    timing_logfile.close()
