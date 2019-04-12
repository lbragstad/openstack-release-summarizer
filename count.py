#! /usr/bin/env python

import argparse
import json

from dogpile.cache import make_region
import requests
import termcolor
import yaml


GERRIT = 'https://review.openstack.org'
# Let's cache so that openstack infra doesn't hate us for *hammering* gerrit.
# This information is relatively static once a release is cut, so we probably
# don't need to pull all the information every time we want this report. A
# little skew in the numbers is accesible for faster run times and less network
# traffic.
region = make_region().configure(
    'dogpile.cache.dbm',
    expiration_time=(7 * 24 * 60 * 60),
    arguments={'filename': 'file.dbm'}
)


class Contributor(object):

    def __init__(self, account_id, name):
        self.account_id = account_id
        self.name = name
        self.review_count = 0
        self.commits = 0


def response_body_to_json(r):
    # The first few characters in response from Gerrit fail JSON rendering,
    # trim them before attempting to parse JSON.
    return json.loads(r.text[4:])


def get_merged_reviews_for_repository(repo, release_name=None):
    reviews = []

    release_date = repo.get('release_date')
    start_date = repo.get('start_date')
    name = repo.get('name')
    # Get all patches merged to master during the Stein development cycle.
    url = (
        GERRIT + '/changes/?q=status:merged+before:' + release_date +
        '+after:' + start_date + '+project:' + name
    )
    r = get(url)
    reviews += response_body_to_json(r)

    if repo.get('stable_branch'):
        url = (
            GERRIT + '/changes/?q=status:merged+project:' + name +
            '+branch:stable/' + release_name
        )
        r = get(url)
        reviews += response_body_to_json(r)

    return reviews


@region.cache_on_arguments()
def get(url):
    return requests.get(url)


@region.cache_on_arguments()
def get_user_by_account_id(account_id):
    resp = requests.get(
        'https://review.openstack.org/accounts/%s' % account_id
    )
    return response_body_to_json(resp)


@region.cache_on_arguments()
def get_review_details(number):
    resp = requests.get(GERRIT + '/changes/%d/detail' % number)
    return response_body_to_json(resp)


def get_args():
    """Get arguments from the user."""
    parser = argparse.ArgumentParser(prog='count-changes')
    parser.add_argument('input', type=str)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--reviews', action='store_true')
    group.add_argument('--commits', action='store_true')
    group.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    return args


def parse_yaml(yaml_file):
    with open(yaml_file, 'r') as f:
        repositories = yaml.safe_load(f)
    return repositories


args = get_args()
collect_reviews = args.reviews
collect_commits = args.commits
summary = args.summary

repositories = parse_yaml(args.input)
IGNORED_USERS = repositories.get('ignored_users')
RELEASE_NAME = repositories.get('release_name')

total_commits = 0
total_reviews = 0
total_additions = 0
total_deletions = 0
contributors = {}
for repository in repositories.get('repositories'):
    project_additions = 0
    project_deletions = 0
    reviews = get_merged_reviews_for_repository(repository, RELEASE_NAME)
    for review in reviews:
        project_additions += review['insertions']
        project_deletions += review['deletions']
        user_id = review['owner']['_account_id']
        if user_id not in contributors.keys() and user_id not in IGNORED_USERS:
            user = get_user_by_account_id(user_id)
            c = Contributor(user_id, user['name'])
            contributors[user_id] = c
            c.commits += 1
            total_commits += 1
        elif user_id in contributors.keys():
            c = contributors[user_id]
            c.commits += 1
            total_commits += 1

        review_number = review['_number']
        patch_owner = review['owner']['_account_id']
        details = get_review_details(review_number)
        for message in details['messages']:
            author_id = None
            if 'author' in message:
                author_id = message['author']['_account_id']
            if author_id and author_id != patch_owner:
                if (author_id not in contributors.keys() and
                        author_id not in IGNORED_USERS):
                    user = get_user_by_account_id(author_id)
                    c = Contributor(author_id, user['name'])
                    contributors[author_id] = c
                    c.review_count += 1
                elif author_id in contributors.keys():
                    c = contributors[author_id]
                    c.review_count += 1

    total_additions += project_additions
    total_deletions += project_deletions


cl = []
for k, v in contributors.items():
    total_reviews += v.review_count
    if v.review_count > 0 or v.commits > 0:
        cl.append(v)

if collect_reviews:
    running_review_percentage = 0.0
    sorted_list = sorted(cl, key=lambda c: c.review_count, reverse=True)
    for i, c in enumerate(sorted_list, 1):
        if c.review_count:
            review_percentage = float((float(c.review_count) / total_reviews))
            running_review_percentage += review_percentage
            # Remove commas from names since we use commas as the delimiter
            name = c.name.replace(',', ' ')
            print(
                '%d,%s,%d,%f,%f' % (
                    i, name, c.review_count, review_percentage,
                    running_review_percentage
                )
            )
elif collect_commits:
    running_commit_percentage = 0.0
    sorted_list = sorted(cl, key=lambda c: c.commits, reverse=True)
    for i, c in enumerate(sorted_list, 1):
        if c.commits:
            commit_percentage = float((float(c.commits) / total_commits))
            running_commit_percentage += commit_percentage
            # Remove commas from names since we use commas as the delimiter
            name = c.name.replace(',', ' ')
            print(
                '%d,%s,%d,%f,%f' % (
                    i, name, c.commits, commit_percentage,
                    running_commit_percentage
                )
            )
elif summary:
    additions = termcolor.colored('+%d' % total_additions, 'green')
    deletions = termcolor.colored('-%d' % total_deletions, 'red')
    print(additions + ' ' + deletions)
    print('%d patches merged' % total_commits)
    print('%d patches reviewed' % total_reviews)
    print('%d total contributors' % len(cl))
