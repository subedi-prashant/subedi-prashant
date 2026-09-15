import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# The workflow must provide a PAT/GitHub App token that can access the profile data.
# The default GITHUB_TOKEN in Actions is not suitable for `user(login: ...)` GraphQL
# queries in this script and will trigger 401 "Bad credentials" responses.
# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers
# Repository permissions: read:Contents, read:Metadata (repo cloning for LOC uses git over HTTPS with this token)
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', '').strip()
if not ACCESS_TOKEN:
    raise RuntimeError('ACCESS_TOKEN is not set. Add a PAT to the repository secrets and pass it to the workflow.')

HEADERS = {
    'Authorization': f'Bearer {ACCESS_TOKEN}',
    'Accept': 'application/vnd.github+json',
}
USER_NAME = os.environ.get('USER_NAME', '').strip()
if not USER_NAME:
    raise RuntimeError('USER_NAME is not set. Configure the workflow to pass the repository owner or username.')
BIRTH_DATE = datetime.datetime(2000, 3, 30)
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_repos_stars(count_type, owner_affiliation, cursor=None, edges=None):
    """
    Uses GitHub's GraphQL v4 API to return my total public repository count or total star count.
    Only counts PUBLIC repositories (private repos are excluded from both counts).
    Paginates through all repositories (100 at a time) so accounts with >100 repos are counted correctly.
    """
    query_count('graph_repos_stars')
    if edges is None:
        edges = []
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation, privacy: PUBLIC) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazerCount
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    repositories = request.json()['data']['user']['repositories']
    edges = edges + repositories['edges']
    if repositories['pageInfo']['hasNextPage']:
        return graph_repos_stars(count_type, owner_affiliation, repositories['pageInfo']['endCursor'], edges)
    if count_type == 'repos':
        return len(edges)
    elif count_type == 'stars':
        return stars_counter(edges)



def get_repo_list(owner_affiliations, cursor=None, edges=None):
    """
    Uses GitHub's GraphQL v4 API to list every repository I own/collaborate on/belong to via an org,
    together with its clone URL and latest commit SHA on the default branch (used for LOC caching).
    """
    query_count('loc_repo_list')
    if edges is None:
        edges = []
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation, isFork: false) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            url
                            isPrivate
                            defaultBranchRef {
                                name
                                target {
                                    ... on Commit {
                                        oid
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliations, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(get_repo_list.__name__, query, variables)
    repositories = request.json()['data']['user']['repositories']
    edges = edges + repositories['edges']
    if repositories['pageInfo']['hasNextPage']:
        return get_repo_list(owner_affiliations, repositories['pageInfo']['endCursor'], edges)
    return edges


# Directories that never contain hand-written source lines worth counting.
LOC_SKIP_DIRS = {
    '.git', 'node_modules', 'dist', 'build', 'out', 'bin', 'obj', '.venv', 'venv',
    '__pycache__', 'target', '.next', 'coverage', '.idea', '.vs', 'vendor',
    'packages', '.nuget', '.angular', '.turbo',
}
# Generated/lock files that don't represent hand-written lines of code.
LOC_SKIP_FILES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'composer.lock',
    'gemfile.lock', 'poetry.lock', 'cargo.lock', 'go.sum',
}
# Binary/asset extensions that shouldn't be treated as text source lines.
LOC_SKIP_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.webp', '.bmp', '.pdf',
    '.woff', '.woff2', '.ttf', '.eot', '.otf', '.zip', '.gz', '.7z', '.rar',
    '.exe', '.dll', '.so', '.dylib', '.class', '.jar', '.pyc', '.pdb', '.min.js',
    '.min.css', '.map', '.lock', '.db', '.sqlite', '.sqlite3', '.mp3', '.mp4',
    '.mov', '.avi', '.wasm',
}


def count_repo_loc(clone_dir):
    """
    Counts current lines of text in tracked files under clone_dir, skipping vendor/build
    directories, lock files, and binary/asset extensions. This reflects the *current*
    lines of code in the repository (not historical additions/deletions).
    """
    import os as _os
    total = 0
    for root, dirs, files in _os.walk(clone_dir):
        dirs[:] = [d for d in dirs if d.lower() not in LOC_SKIP_DIRS]
        for name in files:
            lower = name.lower()
            if lower in LOC_SKIP_FILES:
                continue
            if any(lower.endswith(ext) for ext in LOC_SKIP_EXTENSIONS):
                continue
            path = _os.path.join(root, name)
            try:
                with open(path, 'rb') as f:
                    chunk = f.read(8192)
                    if b'\x00' in chunk:  # crude binary-file detection
                        continue
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    total += sum(1 for _ in f)
            except (OSError, IOError):
                continue
    return total


def current_loc_total(owner_affiliations, cache_file='cache/loc_cache.json'):
    """
    Computes the current total lines of code across all accessible repositories by shallow-cloning
    each repository's default branch and counting tracked source lines. Results are cached per
    repository by its latest commit SHA so unchanged repositories are skipped on subsequent runs.
    """
    import json
    import shutil
    import subprocess
    import tempfile

    try:
        with open(cache_file, 'r') as f:
            cache = json.load(f)
    except (FileNotFoundError, ValueError):
        cache = {}

    repos = get_repo_list(owner_affiliations)
    new_cache = {}
    total_loc = 0
    with tempfile.TemporaryDirectory() as tmp_root:
        for edge in repos:
            node = edge['node']
            name_with_owner = node['nameWithOwner']
            default_branch_ref = node.get('defaultBranchRef')
            if not default_branch_ref or not default_branch_ref.get('target'):
                continue  # empty repository, nothing to count
            sha = default_branch_ref['target']['oid']

            cached_entry = cache.get(name_with_owner)
            if cached_entry and cached_entry.get('sha') == sha:
                loc = cached_entry['loc']
            else:
                clone_dir = os.path.join(tmp_root, hashlib.sha256(name_with_owner.encode('utf-8')).hexdigest())
                clone_url = f'https://x-access-token:{ACCESS_TOKEN}@github.com/{name_with_owner}.git'
                try:
                    subprocess.run(
                        ['git', 'clone', '--depth', '1', '--quiet', '--branch', default_branch_ref['name'], clone_url, clone_dir],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
                    )
                    loc = count_repo_loc(clone_dir)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    loc = cached_entry['loc'] if cached_entry else 0
                finally:
                    shutil.rmtree(clone_dir, ignore_errors=True)

            new_cache[name_with_owner] = {'sha': sha, 'loc': loc}
            total_loc += loc

    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, 'w') as f:
        json.dump(new_cache, f)
    return total_loc


def human_format(number):
    """
    Formats a large integer as a short, human-readable string, e.g. 1_400_000 -> '1.4M', 8_200 -> '8.2K'.
    """
    number = float(number)
    for unit, threshold in (('B', 1_000_000_000), ('M', 1_000_000), ('K', 1_000)):
        if number >= threshold:
            value = number / threshold
            formatted = f'{value:.1f}'.rstrip('0').rstrip('.')
            return f'{formatted}{unit}'
    return str(int(number))


def stars_counter(data):
    """
    Count total stars across my public repositories using the scalar `stargazerCount` field.
    (The `stargazers { totalCount }` connection is access-restricted by GitHub and silently
    returns null for many accounts, which previously made this always report 0.)
    """
    total_stars = 0
    for node in data:
        repo = node.get('node') if isinstance(node, dict) else None
        if not repo:
            continue
        total_stars += int(repo.get('stargazerCount', 0) or 0)
    return total_stars


def svg_overwrite(filename, age_data, repo_data, follower_data):
    """
    Parse SVG files and update elements with uptime, repositories, and followers.
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    find_and_replace(root, 'age_data', str(age_data))
    find_and_replace(root, 'repo_data', str(repo_data))
    find_and_replace(root, 'follower_data', str(follower_data))
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def load_ascii_art(file_path='ascii_art.txt'):
    """
    Loads ASCII art lines from a local text file.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return [line.rstrip('\n') for line in f.readlines()]
    except FileNotFoundError:
        return []


def apply_ascii_art(filename, ascii_lines):
    """
    Replaces the ASCII art tspan lines in the target SVG.
    """
    if not ascii_lines:
        return
    tree = etree.parse(filename)
    root = tree.getroot()
    ascii_text = root.find(".//*[@class='ascii']")
    if ascii_text is None:
        return
    tspans = ascii_text.findall('.//{*}tspan')
    for index, tspan in enumerate(tspans):
        tspan.text = ascii_lines[index] if index < len(ascii_lines) else ''
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    GitHub profile card generator.
    """
    print('Calculation times:')
    age_data, age_time = perf_counter(daily_readme, BIRTH_DATE)
    formatter('uptime calculation', age_time)
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    svg_overwrite('dark_mode.svg', age_data, repo_data, follower_data)
    svg_overwrite('light_mode.svg', age_data, repo_data, follower_data)
    ascii_art_lines = load_ascii_art()
    apply_ascii_art('dark_mode.svg', ascii_art_lines)
    apply_ascii_art('light_mode.svg', ascii_art_lines)

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (age_time + repo_time + follower_time)),
        ' s \033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))