from collections import defaultdict
from pathlib import Path
from typing import TextIO, TypedDict
from tempfile import NamedTemporaryFile
import re
import sys

import click

from bloodhoundcli.hashcat import run_hashcat, decode_password
from bloodhoundcli.db import Database
from bloodhoundcli.util import nthash

DOMAIN_PATTERN = re.compile(r'^[a-z0-9.-]+$')


@click.group()
def ntds() -> None:
    pass


@ntds.command()
@click.argument('ntds', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path), nargs=-1)
@click.option('-p', '--potfile', type=click.Path(file_okay=True, dir_okay=False, path_type=Path), default=Path.home()/'.local/share/hashcat/hashcat.pot')
@click.option('-t', '--task', nargs=2, multiple=True)
@click.option('--pre2k', is_flag=True, default=True)
@click.option('--lmbrute', is_flag=True, default=True)
def crack(ntds: list[str], potfile: Path, task: list[tuple[str, str]], pre2k: bool, lmbrute: bool) -> None:
    with NamedTemporaryFile('w', prefix='hashcat-', suffix='.txt') as userlist, NamedTemporaryFile('w', prefix='hashcat-', suffix='.txt') as lmlist, NamedTemporaryFile('w', prefix='hashcat-', suffix='.txt') as computerlist, NamedTemporaryFile('w', prefix='hashcat-', suffix='.txt') as pre2klist:
        for path in ntds:
            click.echo(f'parsing ntds {path}')
            with open(path, 'r') as input:
                for line in input:
                    identity, _, lmhash, nthash, *_ = line.split(':')
                    if identity.endswith('$'):
                        computerlist.write(nthash)
                        computerlist.write('\n')
                    else:
                        userlist.write(nthash)
                        userlist.write('\n')
                        lmlist.write(lmhash)
                        lmlist.write('\n')

        potfile.parent.mkdir(exist_ok=True)

        if pre2k:
            neo4j = Database.from_env()
            for name in neo4j.execute('MATCH (c:Computer {enabled: true}) WHERE c.lastlogon=0 OR c.lastlogon IS NULL RETURN toLower(c.samaccountname)'):
                name = name.removesuffix('$')
                name = name[:14]
                pre2klist.write(name)
                pre2klist.write('\n')
            click.echo('cracking pre2k computer hashes')
            run_hashcat(hashfile=Path(computerlist.name), potfile=potfile, mode=1000, args=[pre2klist.name, '-O', '-w', '3', '-a', '0'])

        if lmbrute:
            click.echo('cracking user lm hashes')
            run_hashcat(hashfile=Path(lmlist.name), potfile=potfile, mode=3000, args=['--increment', '--increment-min', '1', '-1', '?d?u !#$%*+-??@_.', '?1?1?1?1?1?1?1', '-w', '3', '-a', '3'])

        click.echo('cracking user nt hashes')
        for wordlist, ruleset in task:
            run_hashcat(hashfile=Path(userlist.name), potfile=potfile, mode=1000, args=[wordlist, '-r', ruleset, '-O', '-w', '3', '-a', '0', '--loopback'])


@ntds.command('import')
@click.argument('ntds', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path), nargs=-1)
@click.option('-p', '--potfile', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
def import_(ntds: list[Path], potfile: Path) -> None:
    neo4j = Database.from_env()
    neo4j.create_indices()

    with open(potfile, 'r') as file:
        potdb = parse_potfile(file)
    print(f'loaded {len(potdb)} cracked hashes from {potfile}', file=sys.stderr)

    for path in ntds:
        domain = path.name.removesuffix('.ntds')
        if not DOMAIN_PATTERN.fullmatch(domain):
            raise RuntimeError(f'{path} does not follow the expected naming scheme, a DCSync from the corp.local domain should be named corp.local.ntds')

        path_cleartext = Path(f'{path}.cleartext')
        if path_cleartext.exists():
            with open(path_cleartext, 'r') as file:
                cleardb = parse_ntds_cleartext(file)
            print(f'loaded {len(cleardb)} passwords for {domain} from {path_cleartext}', file=sys.stderr)
            if cleardb:
                import_ntds_cleartext(neo4j, domain, cleardb)

        with open(path, 'r') as file:
            ntdsdb = parse_ntds(file)
        print(f'loaded {len(ntdsdb)} hashes for {domain} from {path}', file=sys.stderr)
        if ntdsdb:
            import_ntds(neo4j, domain, ntdsdb)
        if ntdsdb and potdb:
            import_potfile(neo4j, domain, ntdsdb, potdb)
        # TODO: import kerberos keys into neo4j


class NtdsEntry(TypedDict):
    domain: str
    user: str
    nthash: str
    lmhash: str


def import_ntds_cleartext(neo4j: Database, domain: str, cleardb: dict[str, str]) -> None:
    count = sum(neo4j.execute(
        'UNWIND $rows AS row MERGE (c:Base:Container:Credential {objectid: row[0]}) SET u.nthash=row[0], u.password=row[1], u.cracked=false, u.name=row[2] RETURN count(c)',
        rows=[
            [nthash(password), password, 'Plain Password']
            for password in cleardb.values()
        ],
    ))
    print(f'updated {count} cleartext passwords')
    count = sum(neo4j.execute(
        'UNWIND $rows AS row MATCH (c:Credential {objectid: row[0]}) MATCH (o {domain: row[1], samaccountname: row[2]}) WHERE o:User OR o:Computer MERGE (o)-[r:HasCredential]->(c) MERGE (c)-[s:AssignedTo]->(o) RETURN count(r) + count(s)',
        rows=[
            [nthash(password), domain.upper(), user]
            for user, password in cleardb.items()
        ],
    ))
    print(f'updated {count} credential relationships')


def import_ntds(neo4j: Database, domain: str, ntdsdb: dict[str, list[NtdsEntry]]) -> None:
    count = sum(neo4j.execute(
        'UNWIND $rows AS row MERGE (c:Base:Container:Credential {objectid: row[0]}) SET c.nthash=row[0], c.lmhash=row[1], c.name=row[2] RETURN count(c)',
        rows=[
            [entry['nthash'], entry['lmhash'], 'NT Hash']
            for entries in ntdsdb.values()
            for entry in entries
        ],
    ))
    print(f'updated {count} NT hashes')
    count = sum(neo4j.execute(
        'UNWIND $rows AS row MATCH (c:Credential {objectid: row[0]}) MATCH (o {domain: row[1], samaccountname: row[2]}) WHERE o:User OR o:Computer MERGE (o)-[r:HasCredential]->(c) MERGE (c)-[s:AssignedTo]->(o) RETURN count(r) + count(s)',
        rows=[
            [entry['nthash'], domain.upper(), entry['user']]
            for entries in ntdsdb.values()
            for entry in entries
        ],
    ))
    print(f'updated {count} credential relationships')


def import_potfile(neo4j: Database, domain: str, ntdsdb: dict[str, list[NtdsEntry]], potdb: dict[str, str]) -> None:
    # TODO: import cracked lm passwords into neo4j
    count = sum(neo4j.execute(
        'UNWIND $rows AS row MERGE (c:Base:Container:Credential {objectid: row[0]}) SET c.nthash=row[0], c.password=row[1], c.cracked=true, c.name=row[2] RETURN count(c)',
        rows=[
            [nthash, potdb.get(nthash), 'Cracked NT Hash']
            for nthash in ntdsdb.keys()
            if potdb.get(nthash)  # uncracked hashes are already handled by import_ntds()
        ],
    ))
    print(f'updated {count} cracked NT hashes')
    count = sum(neo4j.execute(
        'UNWIND $rows AS row MATCH (c:Credential {objectid: row[0]}) MATCH (o {domain: row[1], samaccountname: row[2]}) WHERE o:User OR o:Computer MERGE (o)-[r:HasCredential]->(c) MERGE (c)-[s:AssignedTo]->(o) RETURN count(r) + count(s)',
        rows=[
            [nthash, domain.upper(), entry['user']]
            for nthash, entries in ntdsdb.items()
            if potdb.get(nthash)
            for entry in entries
        ],
    ))
    print(f'updated {count} credential relationships')


def parse_ntds_cleartext(file: TextIO) -> dict[str, str]:
    """Returns mapping from user to password."""
    result = {}
    pattern = re.compile(r'^(?:(?P<domain>[^\:]+?)\\)?(?P<user>[^:]+?):CLEARTEXT:(?P<password>.*?)$')
    for linenum, line in enumerate(file, start=1):
        line = line.rstrip()
        match = pattern.search(line)
        if not match:
            print(f'{file.name}:{linenum}: invalid line: {line}', file=sys.stderr)
            continue
        entry = match.groupdict()
        result[entry['user']] = entry['password']
    return result


def parse_ntds(file: TextIO) -> dict[str, list[NtdsEntry]]:
    """Returns mapping from nthash to NTDS entry."""
    result = defaultdict(list)
    pattern = re.compile(r'^(?:(?P<domain>[^\:]+?)\\)?(?P<user>[^:]+?):[^:]+?:(?P<lmhash>[^:]+?):(?P<nthash>[^:]+?):')
    for linenum, line in enumerate(file, start=1):
        line = line.rstrip()
        match = pattern.search(line)
        if not match:
            print(f'{file.name}:{linenum}: invalid line: {line}', file=sys.stderr)
            continue
        entry = match.groupdict()
        result[entry['nthash']].append(entry)
    return result


def parse_potfile(file: TextIO) -> dict[str, str]:
    """Returns mapping from nthash to password."""
    pattern = re.compile(r'^([^\:]{32}?):(.*?)$')
    result = dict()
    for linenum, line in enumerate(file, start=1):
        line = line.rstrip()
        match = pattern.search(line)
        if not match:
            print(f'{file.name}:{linenum}: invalid line: {line}', file=sys.stderr)
            continue
        nthash, password = match.groups()
        result[nthash] = decode_password(password)
    return result
