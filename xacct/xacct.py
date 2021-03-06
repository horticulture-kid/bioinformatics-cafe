#!/usr/bin/env python3

import sys
import subprocess
import csv
import collections
from io import StringIO
import argparse
import os
import datetime
import re

parser = argparse.ArgumentParser(description= """
Execute slurm sacct and return a friendly tabular output. 
Values in MaxRSS and ReqMem are in MB units.

EXAMPLES

xacct.py
xacct.py -- -S 2016-12-01  # Show jobs starting from YYYY-MM-DD
xacct.py -d 1              # Jobs from yesterday onwards

# Sort by memory usage
xacct.py -tsv | tail -n+2 | sort -t'   ' -k4,4n
""", formatter_class= argparse.RawTextHelpFormatter, prog= os.path.basename(__file__))

parser.add_argument('--days', '-d',
                   type= float,
                   default= 0,
                   help='''Show jobs from this many days ago. Default %(default)s
                   ''')

parser.add_argument('--sort', '-s',
                   nargs= '*',
                   type= str,
                   default= ['StateSince'],
                   help='''Sort by these fields. Add suffix '-' to a field name to sort in reverse order.
Without arguments skip sorting altoghether. Default %(default)s.
                   ''')

parser.add_argument('--fromId', '-id',
                   type= int,
                   default= None,
                   help='''Show jobs whose ID is equal or greater than this. Deafult %(default)s
                   ''')

xdef= ['JobID', 'JobName%50', 'NodeList', 'MaxRSS', 'ReqMem', 'AllocCPUS', 'State', 'StateSince', 'Elapsed']
parser.add_argument('--format', '-f',
                   type= str,
                   nargs= '+',
                   default= xdef,
                   help="""Space separated list of columns to print, case insensitive. See 
options --format and --helpformat in `sacct` for details. Use the marker "FMT" 
as a shortcut for the default columns which are:

%s

StateSince is computed within xacct and reports the time since the job has been
in the given state.
    """ % ' '.join(xdef).replace('%', '%%'))

parser.add_argument('--tsv', '-tsv',
                   action= "store_true",
                   help='''Print columns separated by TAB (better for further processing) 
instead of tabulating them (better for eyeballing). This option automatically sets also
--no-color
    ''')

parser.add_argument('--no-color', '-nc',
                   action= "store_true",
                   dest= 'no_color',
                   help='''Do not add color to the output strings. Use this option if you need
to parse the output and the color codes strings get on your way.
    ''')

parser.add_argument('--iso-date', '-dt',
                   action= "store_true",
                   dest= 'iso_date',
                   help='''Leave date-time strings in ISO format as produced by sacct.
By default, date-times are simplified and made more human readable.
    ''')

parser.add_argument('--verbose', '-V',
                   action= "store_true",
                   help='''Verbose for debugging. Print to stderr the sacct command.
                   ''')

parser.add_argument('sacct_args',
                   nargs= "*",
                   help='''Further args to sacct. Sperate them from the other arguments with `--`.
                   ''')

parser.add_argument('--version', '-v', action='version', version='%(prog)s 0.3.0')

args= parser.parse_args()

# -----------------------------------------------------------------------------

def normalizeMem(x):
    """We use binary multiplier instead of decimal to convert kilo and giga to
    mega. I.e.  1024K = 1M. Compare to help for --noconvert option: `Don't
    convert units from their original type (e.g. 2048M won't be converted to
    2G).`. 

    normalizeMem('2G') -> 2048
    """
    if x.strip() == '':
        return '';  
    x= x.strip('n').strip('c') # ReqMem appends 'n' or 'c', see `man sacct` 
    mem= -1
    if x.endswith('K'):
        mem= float(x.strip('K')) * 2**10
    elif x.endswith('M'):
        mem= float(x.strip('M')) * 2**20
    elif x.endswith('G'):
        mem= float(x.strip('G')) * 2**30
    else:
        mem= float(x)
    return round(mem/(2**20))

def getColumnWidths(sacct_out, header, space, no_color):
    """Analyse the table sacct_out (a list of dictionaries) and return 
    a list of column widths. header is a list of header names since also
    these must be considered to get the column widths.
    """
    hdr= collections.OrderedDict()
    for x in header:
        hdr[x]= x
    data= [hdr]
    for x in sacct_out:
        data.append(x)
    widths= []
    for line in data:
        if len(widths) == 0:
            widths= [0] * len(line)
        assert len(widths) == len(line)
        values= list(line.values())
        if not no_color:
            values= colorize(values)
        for i in range(len(line)):
            # Remove ansi colour.
            v= re.sub('\\033\\[[;\\d]*m', '', str(values[i]))
            if header[i].lower() == 'jobid':
                v= re.sub('\.batch$', '', v)
            if widths[i] < len(v):
                widths[i]= len(v)
    widths= [x + space for x in widths]
    return widths

def fillInJob(jobid, batchid):
    if 'MaxRSS' in jobid:
        jobid['MaxRSS']= batchid['MaxRSS']
    return jobid    

def tabulate(line, col_widths, asTsv, no_color):
    if asTsv:
        return '\t'.join([str(x) for x in line])
    if not no_color:
        line= colorize(line)
    exp_widths= []
    for i in range(len(col_widths)):
        offset= len(str(line[i])) - len(re.sub('\\033\\[[;\\d]*m', '', str(line[i])))
        exp_widths.append(col_widths[i] + offset)
    row_fmt= '{:<%s}' * len(exp_widths)
    row_fmt= row_fmt % tuple(exp_widths)
    return row_fmt.format(*line)

def colorize(lst):
    """Add color codes to the printable list
    For codes see https://misc.flogisoft.com/bash/tip_colors_and_formatting
    """
    xcol= []
    for x in lst:
        if x == 'FAILED':
            x= '\033[31mFAILED\033[0m'
        elif x == 'COMPLETED':
            x= '\033[32mCOMPLETED\033[0m'
        elif x == 'RUNNING':
            x= '\033[94mRUNNING\033[0m'
        xcol.append(x)
    return xcol

def simplify_datetime(data, datetime_column):
    """Simplify the datetime string in column _datetime_column_ by removing
    uninformative part(s)
    data:
        List of dictionaries representing the tabular data
    datetime_column:
        Name of column to scan. I.e., key in the dictionaries
    """
    dt= []
    is_date= []
    for x in data:
        try:
            xdt= datetime.datetime.strptime(x[datetime_column], '%Y-%m-%dT%H:%M:%S').date()
            is_date.append(True)
        except:
            xdt= x[datetime_column]
            is_date.append(False)
        dt.append(xdt)
    same_year= True
    same_month= True
    same_day= True
    for i in range(len(dt)):
        x= dt[i]
        if is_date[i]:
            if same_year and x.year != datetime.date.today().year:
                same_year= False
            if same_month and x.month != datetime.date.today().month:
                same_month= False
            if same_day and x.day != datetime.date.today().day:
                same_day= False
    simple= []
    for i in range(len(data)):
        line= data[i]
        if is_date[i]:
            if same_year and same_month and same_day:
                line[datetime_column]= re.sub('.*T', '', line[datetime_column])
            elif same_year and same_month:
                weekday_name= datetime.datetime.strptime(line[datetime_column], '%Y-%m-%dT%H:%M:%S').strftime('%a')
                line[datetime_column]= re.sub('\d\d\d\d-\d\d-', '', line[datetime_column])
                line[datetime_column]= weekday_name + ' ' + re.sub('T', ' ', line[datetime_column])
            else:
                line[datetime_column]= re.sub('T', ' ', line[datetime_column])
        else:
            line[datetime_column]= re.sub('Unknown', '', line[datetime_column])
        simple.append(line)
    return simple


def get_state_time(timepoints):
    """Get the latest of timepoint in the timepoints list. Values in timepoints
    that are not convertible to datetime are skipped
    """
    dt= []
    for x in timepoints:
        try:
            datetime.datetime.strptime(x, '%Y-%m-%dT%H:%M:%S')
            dt.append(x)
        except:
            pass
    if len(dt) == 0:
        return '' 
    else:
        return max(dt)

# -----------------------------------------------------------------------------

starttime= []
if args.days > 0:
    d= datetime.datetime.today() - datetime.timedelta(days= args.days)
    starttime= ['--starttime', d.date().isoformat()]

sacctfmt= [x for x in args.format]

if 'FMT' in args.format:
    args.format= args.format[0:sacctfmt.index('FMT')] + xdef + sacctfmt[sacctfmt.index('FMT')+1:]
while 'FMT' in args.format:
    args.format.remove('FMT')
if 'jobid' not in [x.lower() for x in args.format]:
    args.format.append('JobID')

sacctfmt= [x for x in args.format]

printablefmt= [re.sub('%.*', '', x) for x in args.format]

remove= []
if 'statesince' in [x.lower() for x in sacctfmt]:
    sacctfmt.remove('StateSince') # Make case insesitive!
    if 'start' not in [x.lower() for x in sacctfmt]:
        sacctfmt.append('Start')
        remove.append('Start')
    if 'end' not in [x.lower() for x in sacctfmt]:
        sacctfmt.append('End')
        remove.append('End')
    if 'Submit' not in [x.lower() for x in sacctfmt]:
        sacctfmt.append('Submit')
        remove.append('Submit')

cmd= ['sacct', '--parsable2', '--format=%s' % ','.join(sacctfmt)] + starttime + args.sacct_args
if args.verbose:
    sys.stderr.write(' '.join(cmd) + '\n')
try:
    sacct= subprocess.check_output(cmd, stderr=subprocess.STDOUT)
except subprocess.CalledProcessError as exc:
    print(exc.output.decode().strip())
    print('Exit code %s' % exc.returncode)
    sys.exit(1)

sacct= re.sub('\|None assigned\|', '||', sacct.decode())

reader = csv.DictReader(sacct.split('\n'), delimiter='|')
sacct_out= []
for line in reader:
    if 'MaxRSS' in line:
        line['MaxRSS']= normalizeMem(line['MaxRSS'])
    if 'ReqMem' in line:
        line['ReqMem']= normalizeMem(line['ReqMem'])
    if 'StateSince' in printablefmt:
        line['StateSince']= get_state_time([line['Submit'], line['Start'], line['End']])
    outline= collections.OrderedDict()
    for key in printablefmt:
        outline[key]= '.' if str(line[key]).strip() == '' else line[key]
    sacct_out.append(outline)

# Merge lines with .batch to respective job line
idjob= None
sacct_mrg= []
for line in sacct_out:
    if args.fromId is not None and args.fromId >= int(line['JobID'].replace('.batch', '')):
        continue
    if '.batch' not in line['JobID']:
            if idjob is not None:
                # This line is not a batch job, so the previous line
                # can be printed as it doesn't have an associated batch job.
                ### lst= list(idjob.values())
                ### print(tabulate(lst, col_widths, args.tsv, no_color))
                sacct_mrg.append(idjob)
            idjob= line
    else:
        # This is a batch job. So associate to it the job ID line    
        if idjob['JobID'] == line['JobID'].replace('.batch', ''):
            ## lst= list(fillInJob(idjob, line).values())
            ## print(tabulate(lst, col_widths, args.tsv, no_color))
            sacct_mrg.append(fillInJob(idjob, line))
        else:
            print(idjob)
            print(line)
            raise Exception("Cannot find job id")
        idjob= None
if idjob is not None:
    sacct_mrg.append(idjob)

args.sort.reverse()
for s in args.sort:
    rev= False
    if s.endswith('-'):
        rev= True
        s= re.sub('-$', '', s)
    try:
        sacct_mrg= sorted(sacct_mrg, key=lambda k: k[s], reverse= rev)
    except KeyError:
        sys.stderr.write('\nSort key %s not found\n\n' % s)

if args.tsv:
    no_color= True
else:
    if not args.iso_date:
        try:
            sacct_mrg= simplify_datetime(sacct_mrg, 'StateSince')
        except KeyError:
            pass
        try:
            sacct_mrg= simplify_datetime(sacct_mrg, 'Submit')
        except KeyError:
            pass
        try:
            sacct_mrg= simplify_datetime(sacct_mrg, 'Start')
        except KeyError:
            pass
        try:
            sacct_mrg= simplify_datetime(sacct_mrg, 'End')
        except KeyError:
            pass
    no_color= args.no_color

col_widths= getColumnWidths(sacct_mrg, printablefmt, 2, no_color)

print(tabulate(printablefmt, col_widths, args.tsv, no_color))

idjob= None
try:
    for line in sacct_mrg:
        lst= list(line.values())
        print(tabulate(lst, col_widths, args.tsv, no_color))
    print(tabulate(printablefmt, col_widths, args.tsv, no_color))
except (BrokenPipeError, IOError):
    # This is to avoid stack trace when piping in e.g. `xacct.py | head`
    # See also https://stackoverflow.com/questions/26692284/brokenpipeerror-in-python
    pass
