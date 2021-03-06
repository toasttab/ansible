# -*- coding: utf-8 -*-
# Copyright: (c) 2019, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import copy
import json
import os
import pytest
import re
import shutil
import tarfile
import yaml

from io import BytesIO, StringIO
from units.compat.mock import MagicMock

import ansible.module_utils.six.moves.urllib.error as urllib_error

from ansible import context
from ansible.cli.galaxy import GalaxyCLI
from ansible.errors import AnsibleError
from ansible.galaxy import collection, api
from ansible.module_utils._text import to_bytes, to_native, to_text
from ansible.utils import context_objects as co
from ansible.utils.display import Display


def call_galaxy_cli(args):
    orig = co.GlobalCLIArgs._Singleton__instance
    co.GlobalCLIArgs._Singleton__instance = None
    try:
        GalaxyCLI(args=['ansible-galaxy', 'collection'] + args).run()
    finally:
        co.GlobalCLIArgs._Singleton__instance = orig


def artifact_json(namespace, name, version, dependencies, server):
    json_str = json.dumps({
        'artifact': {
            'filename': '%s-%s-%s.tar.gz' % (namespace, name, version),
            'sha256': '2d76f3b8c4bab1072848107fb3914c345f71a12a1722f25c08f5d3f51f4ab5fd',
            'size': 1234,
        },
        'download_url': '%s/download/%s-%s-%s.tar.gz' % (server, namespace, name, version),
        'metadata': {
            'namespace': namespace,
            'name': name,
            'dependencies': dependencies,
        },
        'version': version
    })
    return to_text(json_str)


def artifact_versions_json(namespace, name, versions, galaxy_api, available_api_versions=None):
    results = []
    available_api_versions = available_api_versions or {}
    api_version = 'v2'
    if 'v3' in available_api_versions:
        api_version = 'v3'
    for version in versions:
        results.append({
            'href': '%s/api/%s/%s/%s/versions/%s/' % (galaxy_api.api_server, api_version, namespace, name, version),
            'version': version,
        })

    if api_version == 'v2':
        json_str = json.dumps({
            'count': len(versions),
            'next': None,
            'previous': None,
            'results': results
        })

    if api_version == 'v3':
        response = {'meta': {'count': len(versions)},
                    'data': results,
                    'links': {'first': None,
                              'last': None,
                              'next': None,
                              'previous': None},
                    }
        json_str = json.dumps(response)
    return to_text(json_str)


def error_json(galaxy_api, errors_to_return=None, available_api_versions=None):
    errors_to_return = errors_to_return or []
    available_api_versions = available_api_versions or {}

    response = {}

    api_version = 'v2'
    if 'v3' in available_api_versions:
        api_version = 'v3'

    if api_version == 'v2':
        assert len(errors_to_return) <= 1
        if errors_to_return:
            response = errors_to_return[0]

    if api_version == 'v3':
        response['errors'] = errors_to_return

    json_str = json.dumps(response)
    return to_text(json_str)


@pytest.fixture(autouse='function')
def reset_cli_args():
    co.GlobalCLIArgs._Singleton__instance = None
    yield
    co.GlobalCLIArgs._Singleton__instance = None


@pytest.fixture()
def collection_artifact(request, tmp_path_factory):
    test_dir = to_text(tmp_path_factory.mktemp('test-ÅÑŚÌβŁÈ Collections Input'))
    namespace = 'ansible_namespace'
    collection = 'collection'

    skeleton_path = os.path.join(os.path.dirname(os.path.split(__file__)[0]), 'cli', 'test_data', 'collection_skeleton')
    collection_path = os.path.join(test_dir, namespace, collection)

    call_galaxy_cli(['init', '%s.%s' % (namespace, collection), '-c', '--init-path', test_dir,
                     '--collection-skeleton', skeleton_path])
    dependencies = getattr(request, 'param', None)
    if dependencies:
        galaxy_yml = os.path.join(collection_path, 'galaxy.yml')
        with open(galaxy_yml, 'rb+') as galaxy_obj:
            existing_yaml = yaml.safe_load(galaxy_obj)
            existing_yaml['dependencies'] = dependencies

            galaxy_obj.seek(0)
            galaxy_obj.write(to_bytes(yaml.safe_dump(existing_yaml)))
            galaxy_obj.truncate()

    call_galaxy_cli(['build', collection_path, '--output-path', test_dir])

    collection_tar = os.path.join(test_dir, '%s-%s-0.1.0.tar.gz' % (namespace, collection))
    return to_bytes(collection_path), to_bytes(collection_tar)


@pytest.fixture()
def galaxy_server():
    context.CLIARGS._store = {'ignore_certs': False}
    galaxy_api = api.GalaxyAPI(None, 'test_server', 'https://galaxy.ansible.com')
    return galaxy_api


def test_build_requirement_from_path(collection_artifact):
    actual = collection.CollectionRequirement.from_path(collection_artifact[0], True)

    assert actual.namespace == u'ansible_namespace'
    assert actual.name == u'collection'
    assert actual.b_path == collection_artifact[0]
    assert actual.api is None
    assert actual.skip is True
    assert actual.versions == set([u'*'])
    assert actual.latest_version == u'*'
    assert actual.dependencies == {}


def test_build_requirement_from_path_with_manifest(collection_artifact):
    manifest_path = os.path.join(collection_artifact[0], b'MANIFEST.json')
    manifest_value = json.dumps({
        'collection_info': {
            'namespace': 'namespace',
            'name': 'name',
            'version': '1.1.1',
            'dependencies': {
                'ansible_namespace.collection': '*'
            }
        }
    })
    with open(manifest_path, 'wb') as manifest_obj:
        manifest_obj.write(to_bytes(manifest_value))

    actual = collection.CollectionRequirement.from_path(collection_artifact[0], True)

    # While the folder name suggests a different collection, we treat MANIFEST.json as the source of truth.
    assert actual.namespace == u'namespace'
    assert actual.name == u'name'
    assert actual.b_path == collection_artifact[0]
    assert actual.api is None
    assert actual.skip is True
    assert actual.versions == set([u'1.1.1'])
    assert actual.latest_version == u'1.1.1'
    assert actual.dependencies == {'ansible_namespace.collection': '*'}


def test_build_requirement_from_path_invalid_manifest(collection_artifact):
    manifest_path = os.path.join(collection_artifact[0], b'MANIFEST.json')
    with open(manifest_path, 'wb') as manifest_obj:
        manifest_obj.write(b"not json")

    expected = "Collection file at '%s' does not contain a valid json string." % to_native(manifest_path)
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_path(collection_artifact[0], True)


def test_build_requirement_from_tar(collection_artifact):
    actual = collection.CollectionRequirement.from_tar(collection_artifact[1], True, True)

    assert actual.namespace == u'ansible_namespace'
    assert actual.name == u'collection'
    assert actual.b_path == collection_artifact[1]
    assert actual.api is None
    assert actual.skip is False
    assert actual.versions == set([u'0.1.0'])
    assert actual.latest_version == u'0.1.0'
    assert actual.dependencies == {}


def test_build_requirement_from_tar_fail_not_tar(tmp_path_factory):
    test_dir = to_bytes(tmp_path_factory.mktemp('test-ÅÑŚÌβŁÈ Collections Input'))
    test_file = os.path.join(test_dir, b'fake.tar.gz')
    with open(test_file, 'wb') as test_obj:
        test_obj.write(b"\x00\x01\x02\x03")

    expected = "Collection artifact at '%s' is not a valid tar file." % to_native(test_file)
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_tar(test_file, True, True)


def test_build_requirement_from_tar_no_manifest(tmp_path_factory):
    test_dir = to_bytes(tmp_path_factory.mktemp('test-ÅÑŚÌβŁÈ Collections Input'))

    json_data = to_bytes(json.dumps(
        {
            'files': [],
            'format': 1,
        }
    ))

    tar_path = os.path.join(test_dir, b'ansible-collections.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tfile:
        b_io = BytesIO(json_data)
        tar_info = tarfile.TarInfo('FILES.json')
        tar_info.size = len(json_data)
        tar_info.mode = 0o0644
        tfile.addfile(tarinfo=tar_info, fileobj=b_io)

    expected = "Collection at '%s' does not contain the required file MANIFEST.json." % to_native(tar_path)
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_tar(tar_path, True, True)


def test_build_requirement_from_tar_no_files(tmp_path_factory):
    test_dir = to_bytes(tmp_path_factory.mktemp('test-ÅÑŚÌβŁÈ Collections Input'))

    json_data = to_bytes(json.dumps(
        {
            'collection_info': {},
        }
    ))

    tar_path = os.path.join(test_dir, b'ansible-collections.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tfile:
        b_io = BytesIO(json_data)
        tar_info = tarfile.TarInfo('MANIFEST.json')
        tar_info.size = len(json_data)
        tar_info.mode = 0o0644
        tfile.addfile(tarinfo=tar_info, fileobj=b_io)

    expected = "Collection at '%s' does not contain the required file FILES.json." % to_native(tar_path)
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_tar(tar_path, True, True)


def test_build_requirement_from_tar_invalid_manifest(tmp_path_factory):
    test_dir = to_bytes(tmp_path_factory.mktemp('test-ÅÑŚÌβŁÈ Collections Input'))

    json_data = b"not a json"

    tar_path = os.path.join(test_dir, b'ansible-collections.tar.gz')
    with tarfile.open(tar_path, 'w:gz') as tfile:
        b_io = BytesIO(json_data)
        tar_info = tarfile.TarInfo('MANIFEST.json')
        tar_info.size = len(json_data)
        tar_info.mode = 0o0644
        tfile.addfile(tarinfo=tar_info, fileobj=b_io)

    expected = "Collection tar file member MANIFEST.json does not contain a valid json string."
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_tar(tar_path, True, True)


@pytest.mark.parametrize("api_version,exp_api_url", [
    ('v2', '/api/v2/collections/namespace/collection/versions/'),
    ('v3', '/api/v3/collections/namespace/collection/versions/')
])
def test_build_requirement_from_name(api_version, exp_api_url, galaxy_server, monkeypatch):
    mock_avail_ver = MagicMock()
    avail_api_versions = {api_version: '/api/%s' % api_version}
    mock_avail_ver.return_value = avail_api_versions
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    json_str = artifact_versions_json('namespace', 'collection', ['2.1.9', '2.1.10'], galaxy_server, avail_api_versions)
    mock_open = MagicMock()
    mock_open.return_value = StringIO(json_str)

    monkeypatch.setattr(collection, 'open_url', mock_open)

    actual = collection.CollectionRequirement.from_name('namespace.collection', [galaxy_server], '*', True, True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'2.1.9', u'2.1.10'])
    assert actual.latest_version == u'2.1.10'
    assert actual.dependencies is None

    assert mock_open.call_count == 1
    assert mock_open.mock_calls[0][1][0] == '%s%s' % (galaxy_server.api_server, exp_api_url)
    assert mock_open.mock_calls[0][2] == {'validate_certs': True, "headers": {}}


@pytest.mark.parametrize("api_version,exp_api_url", [
    ('v2', '/api/v2/collections/namespace/collection/versions/'),
    ('v3', '/api/v3/collections/namespace/collection/versions/')
])
def test_build_requirement_from_name_with_prerelease(api_version, exp_api_url, galaxy_server, monkeypatch):
    mock_avail_ver = MagicMock()
    avail_api_versions = {api_version: '/api/%s' % api_version}
    mock_avail_ver.return_value = avail_api_versions
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    json_str = artifact_versions_json('namespace', 'collection', ['1.0.1', '2.0.1-beta.1', '2.0.1'],
                                      galaxy_server, avail_api_versions)
    mock_open = MagicMock()
    mock_open.return_value = StringIO(json_str)

    monkeypatch.setattr(collection, 'open_url', mock_open)

    actual = collection.CollectionRequirement.from_name('namespace.collection', [galaxy_server], '*', True, True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'1.0.1', u'2.0.1'])
    assert actual.latest_version == u'2.0.1'
    assert actual.dependencies is None

    assert mock_open.call_count == 1
    assert mock_open.mock_calls[0][1][0] == '%s%s' % (galaxy_server.api_server, exp_api_url)
    assert mock_open.mock_calls[0][2] == {'validate_certs': True, "headers": {}}


@pytest.mark.parametrize("api_version,exp_api_url", [
    ('v2', '/api/v2/collections/namespace/collection/versions/2.0.1-beta.1/'),
    ('v3', '/api/v3/collections/namespace/collection/versions/2.0.1-beta.1/')
])
def test_build_requirment_from_name_with_prerelease_explicit(api_version, exp_api_url, galaxy_server, monkeypatch):
    mock_avail_ver = MagicMock()
    avail_api_versions = {api_version: '/api/%s' % api_version}
    mock_avail_ver.return_value = avail_api_versions
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    json_str = artifact_json('namespace', 'collection', '2.0.1-beta.1', {}, galaxy_server.api_server)
    mock_open = MagicMock()
    mock_open.side_effect = (
        StringIO(json_str),
    )

    monkeypatch.setattr(collection, 'open_url', mock_open)

    actual = collection.CollectionRequirement.from_name('namespace.collection', [galaxy_server], '2.0.1-beta.1', True,
                                                        True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'2.0.1-beta.1'])
    assert actual.latest_version == u'2.0.1-beta.1'
    assert actual.dependencies == {}

    assert mock_open.call_count == 1
    assert mock_open.mock_calls[0][1][0] == '%s%s' % (galaxy_server.api_server, exp_api_url)
    assert mock_open.mock_calls[0][2] == {'validate_certs': True, "headers": {}}


@pytest.mark.parametrize("api_version,exp_api_url", [
    ('v2', '/api/v2/collections/namespace/collection/versions/'),
    ('v3', '/api/v3/collections/namespace/collection/versions/')
])
def test_build_requirement_from_name_second_server(api_version, exp_api_url, galaxy_server, monkeypatch):
    mock_avail_ver = MagicMock()
    avail_api_versions = {api_version: '/api/%s' % api_version}
    mock_avail_ver.return_value = avail_api_versions
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    json_str = artifact_versions_json('namespace', 'collection', ['1.0.1', '1.0.2', '1.0.3'], galaxy_server, avail_api_versions)
    mock_open = MagicMock()
    mock_open.side_effect = (
        urllib_error.HTTPError('https://galaxy.server.com', 404, 'msg', {}, None),
        StringIO(json_str)
    )

    monkeypatch.setattr(collection, 'open_url', mock_open)

    broken_server = copy.copy(galaxy_server)
    broken_server.api_server = 'https://broken.com/'
    actual = collection.CollectionRequirement.from_name('namespace.collection', [broken_server, galaxy_server],
                                                        '>1.0.1', False, True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    # assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'1.0.2', u'1.0.3'])
    assert actual.latest_version == u'1.0.3'
    assert actual.dependencies is None

    assert mock_open.call_count == 2
    assert mock_open.mock_calls[0][1][0] == u"https://broken.com%s" % exp_api_url
    assert mock_open.mock_calls[1][1][0] == u"%s%s" % (galaxy_server.api_server, exp_api_url)
    assert mock_open.mock_calls[1][2] == {'validate_certs': True, "headers": {}}


def test_build_requirement_from_name_missing(galaxy_server, monkeypatch):
    mock_open = MagicMock()
    mock_open.side_effect = urllib_error.HTTPError('https://galaxy.server.com', 404, 'msg', {}, None)

    monkeypatch.setattr(collection, 'open_url', mock_open)

    mock_avail_ver = MagicMock()
    mock_avail_ver.return_value = {'v2': '/api/v2'}
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    expected = "Failed to find collection namespace.collection:*"
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_name('namespace.collection',
                                                   [galaxy_server, galaxy_server], '*', False, True)


@pytest.mark.parametrize("api_version,errors_to_return,expected", [
    ('v2',
     [],
     'Error fetching info for .*\\..* \\(HTTP Code: 400, Message: Unknown error returned by Galaxy server. Code: Unknown\\)'),
    ('v2',
     [{'message': 'Polarization error. Try flipping it over.', 'code': 'polarization_error'}],
     'Error fetching info for .*\\..* \\(HTTP Code: 400, Message: Polarization error. Try flipping it over. Code: polarization_error\\)'),
    ('v3',
     [],
     'Error fetching info for .*\\..* \\(HTTP Code: 400, Message: Unknown error returned by Galaxy server. Code: Unknown\\)'),
    ('v3',
     [{'code': 'invalid_param', 'detail': '"easy" is not a valid query param'}],
     'Error fetching info for .*\\..* \\(HTTP Code: 400, Message: "easy" is not a valid query param Code: invalid_param\\)'),
])
def test_build_requirement_from_name_400_bad_request(api_version, errors_to_return, expected, galaxy_server, monkeypatch):
    mock_avail_ver = MagicMock()
    available_api_versions = {api_version: '/api/%s' % api_version}
    mock_avail_ver.return_value = available_api_versions
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    json_str = error_json(galaxy_server, errors_to_return=errors_to_return, available_api_versions=available_api_versions)

    mock_open = MagicMock()
    monkeypatch.setattr(collection, 'open_url', mock_open)
    mock_open.side_effect = urllib_error.HTTPError('https://galaxy.server.com', 400, 'msg', {}, StringIO(json_str))

    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_name('namespace.collection',
                                                   [galaxy_server, galaxy_server], '*', False)


@pytest.mark.parametrize("api_version,errors_to_return,expected", [
    ('v2',
     [],
     'Error fetching info for .*\\..* \\(HTTP Code: 401, Message: Unknown error returned by Galaxy server. Code: Unknown\\)'),
    ('v3',
     [],
     'Error fetching info for .*\\..* \\(HTTP Code: 401, Message: Unknown error returned by Galaxy server. Code: Unknown\\)'),
    ('v3',
     [{'code': 'unauthorized', 'detail': 'The request was not authorized'}],
     'Error fetching info for .*\\..* \\(HTTP Code: 401, Message: The request was not authorized Code: unauthorized\\)'),
])
def test_build_requirement_from_name_401_unauthorized(api_version, errors_to_return, expected, galaxy_server, monkeypatch):
    mock_avail_ver = MagicMock()
    available_api_versions = {api_version: '/api/%s' % api_version}
    mock_avail_ver.return_value = available_api_versions
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    json_str = error_json(galaxy_server, errors_to_return=errors_to_return, available_api_versions=available_api_versions)

    mock_open = MagicMock()
    monkeypatch.setattr(collection, 'open_url', mock_open)
    mock_open.side_effect = urllib_error.HTTPError('https://galaxy.server.com', 401, 'msg', {}, StringIO(json_str))

    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement.from_name('namespace.collection',
                                                   [galaxy_server, galaxy_server], '*', False)


def test_build_requirement_from_name_single_version(galaxy_server, monkeypatch):
    json_str = artifact_json('namespace', 'collection', '2.0.0', {}, galaxy_server.api_server)
    mock_open = MagicMock()
    mock_open.return_value = StringIO(json_str)

    monkeypatch.setattr(collection, 'open_url', mock_open)

    mock_avail_ver = MagicMock()
    mock_avail_ver.return_value = {'v2': '/api/v2'}
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    actual = collection.CollectionRequirement.from_name('namespace.collection', [galaxy_server], '2.0.0', True, True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'2.0.0'])
    assert actual.latest_version == u'2.0.0'
    assert actual.dependencies == {}

    assert mock_open.call_count == 1
    assert mock_open.mock_calls[0][1][0] == u"%s/api/v2/collections/namespace/collection/versions/2.0.0/" \
        % galaxy_server.api_server
    assert mock_open.mock_calls[0][2] == {'validate_certs': True, "headers": {}}


def test_build_requirement_from_name_multiple_versions_one_match(galaxy_server, monkeypatch):
    json_str1 = artifact_versions_json('namespace', 'collection', ['2.0.0', '2.0.1', '2.0.2'],
                                       galaxy_server)
    json_str2 = artifact_json('namespace', 'collection', '2.0.1', {}, galaxy_server.api_server)
    mock_open = MagicMock()
    mock_open.side_effect = (StringIO(json_str1), StringIO(json_str2))

    monkeypatch.setattr(collection, 'open_url', mock_open)

    mock_avail_ver = MagicMock()
    mock_avail_ver.return_value = {'v2': '/api/v2'}
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    actual = collection.CollectionRequirement.from_name('namespace.collection', [galaxy_server], '>=2.0.1,<2.0.2',
                                                        True, True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'2.0.1'])
    assert actual.latest_version == u'2.0.1'
    assert actual.dependencies == {}

    assert mock_open.call_count == 2
    assert mock_open.mock_calls[0][1][0] == u"%s/api/v2/collections/namespace/collection/versions/" \
        % galaxy_server.api_server
    assert mock_open.mock_calls[0][2] == {'validate_certs': True, "headers": {}}
    assert mock_open.mock_calls[1][1][0] == u"%s/api/v2/collections/namespace/collection/versions/2.0.1/" \
        % galaxy_server.api_server
    assert mock_open.mock_calls[1][2] == {'validate_certs': True, "headers": {}}


def test_build_requirement_from_name_multiple_version_results(galaxy_server, monkeypatch):
    json_str1 = json.dumps({
        'count': 6,
        'next': '%s/api/v2/collections/namespace/collection/versions/?page=2' % galaxy_server.api_server,
        'previous': None,
        'results': [
            {
                'href': '%s/api/v2/collections/namespace/collection/versions/2.0.0/' % galaxy_server.api_server,
                'version': '2.0.0',
            },
            {
                'href': '%s/api/v2/collections/namespace/collection/versions/2.0.1/' % galaxy_server.api_server,
                'version': '2.0.1',
            },
            {
                'href': '%s/api/v2/collections/namespace/collection/versions/2.0.2/' % galaxy_server.api_server,
                'version': '2.0.2',
            },
        ]
    })
    json_str2 = json.dumps({
        'count': 6,
        'next': None,
        'previous': '%s/api/v2/collections/namespace/collection/versions/?page=1' % galaxy_server.api_server,
        'results': [
            {
                'href': '%s/api/v2/collections/namespace/collection/versions/2.0.3/' % galaxy_server.api_server,
                'version': '2.0.3',
            },
            {
                'href': '%s/api/v2/collections/namespace/collection/versions/2.0.4/' % galaxy_server.api_server,
                'version': '2.0.4',
            },
            {
                'href': '%s/api/v2/collections/namespace/collection/versions/2.0.5/' % galaxy_server.api_server,
                'version': '2.0.5',
            },
        ]
    })
    mock_open = MagicMock()
    mock_open.side_effect = (StringIO(to_text(json_str1)), StringIO(to_text(json_str2)))

    monkeypatch.setattr(collection, 'open_url', mock_open)

    mock_avail_ver = MagicMock()
    mock_avail_ver.return_value = {'v2': '/api/v2'}
    monkeypatch.setattr(collection, 'get_available_api_versions', mock_avail_ver)

    actual = collection.CollectionRequirement.from_name('namespace.collection', [galaxy_server], '!=2.0.2',
                                                        True, True)

    assert actual.namespace == u'namespace'
    assert actual.name == u'collection'
    assert actual.b_path is None
    assert actual.api == galaxy_server
    assert actual.skip is False
    assert actual.versions == set([u'2.0.0', u'2.0.1', u'2.0.3', u'2.0.4', u'2.0.5'])
    assert actual.latest_version == u'2.0.5'
    assert actual.dependencies is None

    assert mock_open.call_count == 2
    assert mock_open.mock_calls[0][1][0] == u"%s/api/v2/collections/namespace/collection/versions/" \
        % galaxy_server.api_server
    assert mock_open.mock_calls[0][2] == {'validate_certs': True, "headers": {}}
    assert mock_open.mock_calls[1][1][0] == u"%s/api/v2/collections/namespace/collection/versions/?page=2" \
        % galaxy_server.api_server
    assert mock_open.mock_calls[1][2] == {'validate_certs': True, "headers": {}}


@pytest.mark.parametrize('versions, requirement, expected_filter, expected_latest', [
    [['1.0.0', '1.0.1'], '*', ['1.0.0', '1.0.1'], '1.0.1'],
    [['1.0.0', '1.0.5', '1.1.0'], '>1.0.0,<1.1.0', ['1.0.5'], '1.0.5'],
    [['1.0.0', '1.0.5', '1.1.0'], '>1.0.0,<=1.0.5', ['1.0.5'], '1.0.5'],
    [['1.0.0', '1.0.5', '1.1.0'], '>=1.1.0', ['1.1.0'], '1.1.0'],
    [['1.0.0', '1.0.5', '1.1.0'], '!=1.1.0', ['1.0.0', '1.0.5'], '1.0.5'],
    [['1.0.0', '1.0.5', '1.1.0'], '==1.0.5', ['1.0.5'], '1.0.5'],
    [['1.0.0', '1.0.5', '1.1.0'], '1.0.5', ['1.0.5'], '1.0.5'],
    [['1.0.0', '2.0.0', '3.0.0'], '>=2', ['2.0.0', '3.0.0'], '3.0.0'],
])
def test_add_collection_requirements(versions, requirement, expected_filter, expected_latest):
    req = collection.CollectionRequirement('namespace', 'name', None, 'https://galaxy.com', versions, requirement,
                                           False)
    assert req.versions == set(expected_filter)
    assert req.latest_version == expected_latest


def test_add_collection_requirement_to_unknown_installed_version():
    req = collection.CollectionRequirement('namespace', 'name', None, 'https://galaxy.com', ['*'], '*', False,
                                           skip=True)

    expected = "Cannot meet requirement namespace.name:1.0.0 as it is already installed at version 'unknown'."
    with pytest.raises(AnsibleError, match=expected):
        req.add_requirement(str(req), '1.0.0')


def test_add_collection_wildcard_requirement_to_unknown_installed_version():
    req = collection.CollectionRequirement('namespace', 'name', None, 'https://galaxy.com', ['*'], '*', False,
                                           skip=True)
    req.add_requirement(str(req), '*')

    assert req.versions == set('*')
    assert req.latest_version == '*'


def test_add_collection_requirement_with_conflict(galaxy_server):
    expected = "Cannot meet requirement ==1.0.2 for dependency namespace.name from source '%s'. Available versions " \
               "before last requirement added: 1.0.0, 1.0.1\n" \
               "Requirements from:\n" \
               "\tbase - 'namespace.name:==1.0.2'" % galaxy_server.api_server
    with pytest.raises(AnsibleError, match=expected):
        collection.CollectionRequirement('namespace', 'name', None, galaxy_server, ['1.0.0', '1.0.1'], '==1.0.2',
                                         False)


def test_add_requirement_to_existing_collection_with_conflict(galaxy_server):
    req = collection.CollectionRequirement('namespace', 'name', None, galaxy_server, ['1.0.0', '1.0.1'], '*', False)

    expected = "Cannot meet dependency requirement 'namespace.name:1.0.2' for collection namespace.collection2 from " \
               "source '%s'. Available versions before last requirement added: 1.0.0, 1.0.1\n" \
               "Requirements from:\n" \
               "\tbase - 'namespace.name:*'\n" \
               "\tnamespace.collection2 - 'namespace.name:1.0.2'" % galaxy_server.api_server
    with pytest.raises(AnsibleError, match=re.escape(expected)):
        req.add_requirement('namespace.collection2', '1.0.2')


def test_add_requirement_to_installed_collection_with_conflict():
    source = 'https://galaxy.ansible.com'
    req = collection.CollectionRequirement('namespace', 'name', None, source, ['1.0.0', '1.0.1'], '*', False,
                                           skip=True)

    expected = "Cannot meet requirement namespace.name:1.0.2 as it is already installed at version '1.0.1'. " \
               "Use --force to overwrite"
    with pytest.raises(AnsibleError, match=re.escape(expected)):
        req.add_requirement(None, '1.0.2')


def test_add_requirement_to_installed_collection_with_conflict_as_dep():
    source = 'https://galaxy.ansible.com'
    req = collection.CollectionRequirement('namespace', 'name', None, source, ['1.0.0', '1.0.1'], '*', False,
                                           skip=True)

    expected = "Cannot meet requirement namespace.name:1.0.2 as it is already installed at version '1.0.1'. " \
               "Use --force-with-deps to overwrite"
    with pytest.raises(AnsibleError, match=re.escape(expected)):
        req.add_requirement('namespace.collection2', '1.0.2')


def test_install_skipped_collection(monkeypatch):
    mock_display = MagicMock()
    monkeypatch.setattr(Display, 'display', mock_display)

    req = collection.CollectionRequirement('namespace', 'name', None, 'source', ['1.0.0'], '*', False, skip=True)
    req.install(None, None)

    assert mock_display.call_count == 1
    assert mock_display.mock_calls[0][1][0] == "Skipping 'namespace.name' as it is already installed"


def test_install_collection(collection_artifact, monkeypatch):
    mock_display = MagicMock()
    monkeypatch.setattr(Display, 'display', mock_display)

    collection_tar = collection_artifact[1]
    output_path = os.path.join(os.path.split(collection_tar)[0], b'output')
    collection_path = os.path.join(output_path, b'ansible_namespace', b'collection')
    os.makedirs(os.path.join(collection_path, b'delete_me'))  # Create a folder to verify the install cleans out the dir

    temp_path = os.path.join(os.path.split(collection_tar)[0], b'temp')
    os.makedirs(temp_path)

    req = collection.CollectionRequirement.from_tar(collection_tar, True, True)
    req.install(to_text(output_path), temp_path)

    # Ensure the temp directory is empty, nothing is left behind
    assert os.listdir(temp_path) == []

    actual_files = os.listdir(collection_path)
    actual_files.sort()
    assert actual_files == [b'FILES.json', b'MANIFEST.json', b'README.md', b'docs', b'playbooks', b'plugins', b'roles']

    assert mock_display.call_count == 1
    assert mock_display.mock_calls[0][1][0] == "Installing 'ansible_namespace.collection:0.1.0' to '%s'" \
        % to_text(collection_path)


def test_install_collection_with_download(galaxy_server, collection_artifact, monkeypatch):
    collection_tar = collection_artifact[1]
    output_path = os.path.join(os.path.split(collection_tar)[0], b'output')
    collection_path = os.path.join(output_path, b'ansible_namespace', b'collection')

    mock_display = MagicMock()
    monkeypatch.setattr(Display, 'display', mock_display)

    mock_download = MagicMock()
    mock_download.return_value = collection_tar
    monkeypatch.setattr(collection, '_download_file', mock_download)

    temp_path = os.path.join(os.path.split(collection_tar)[0], b'temp')
    os.makedirs(temp_path)

    req = collection.CollectionRequirement('ansible_namespace', 'collection', None, galaxy_server,
                                           ['0.1.0'], '*', False)
    req._galaxy_info = {
        'download_url': 'https://downloadme.com',
        'artifact': {
            'sha256': 'myhash',
        },
    }
    req.install(to_text(output_path), temp_path)

    # Ensure the temp directory is empty, nothing is left behind
    assert os.listdir(temp_path) == []

    actual_files = os.listdir(collection_path)
    actual_files.sort()
    assert actual_files == [b'FILES.json', b'MANIFEST.json', b'README.md', b'docs', b'playbooks', b'plugins', b'roles']

    assert mock_display.call_count == 1
    assert mock_display.mock_calls[0][1][0] == "Installing 'ansible_namespace.collection:0.1.0' to '%s'" \
        % to_text(collection_path)

    assert mock_download.call_count == 1
    assert mock_download.mock_calls[0][1][0] == 'https://downloadme.com'
    assert mock_download.mock_calls[0][1][1] == temp_path
    assert mock_download.mock_calls[0][1][2] == 'myhash'
    assert mock_download.mock_calls[0][1][3] is True


def test_install_collections_from_tar(collection_artifact, monkeypatch):
    collection_path, collection_tar = collection_artifact
    temp_path = os.path.split(collection_tar)[0]
    shutil.rmtree(collection_path)

    mock_display = MagicMock()
    monkeypatch.setattr(Display, 'display', mock_display)

    collection.install_collections([(to_text(collection_tar), '*', None,)], to_text(temp_path),
                                   [u'https://galaxy.ansible.com'], True, False, False, False, False)

    assert os.path.isdir(collection_path)

    actual_files = os.listdir(collection_path)
    actual_files.sort()
    assert actual_files == [b'FILES.json', b'MANIFEST.json', b'README.md', b'docs', b'playbooks', b'plugins', b'roles']

    with open(os.path.join(collection_path, b'MANIFEST.json'), 'rb') as manifest_obj:
        actual_manifest = json.loads(to_text(manifest_obj.read()))

    assert actual_manifest['collection_info']['namespace'] == 'ansible_namespace'
    assert actual_manifest['collection_info']['name'] == 'collection'
    assert actual_manifest['collection_info']['version'] == '0.1.0'

    # Filter out the progress cursor display calls.
    display_msgs = [m[1][0] for m in mock_display.mock_calls if 'newline' not in m[2]]
    assert len(display_msgs) == 3
    assert display_msgs[0] == "Process install dependency map"
    assert display_msgs[1] == "Starting collection install process"
    assert display_msgs[2] == "Installing 'ansible_namespace.collection:0.1.0' to '%s'" % to_text(collection_path)


def test_install_collections_existing_without_force(collection_artifact, monkeypatch):
    collection_path, collection_tar = collection_artifact
    temp_path = os.path.split(collection_tar)[0]

    mock_display = MagicMock()
    monkeypatch.setattr(Display, 'display', mock_display)

    # If we don't delete collection_path it will think the original build skeleton is installed so we expect a skip
    collection.install_collections([(to_text(collection_tar), '*', None,)], to_text(temp_path),
                                   [u'https://galaxy.ansible.com'], True, False, False, False, False)

    assert os.path.isdir(collection_path)

    actual_files = os.listdir(collection_path)
    actual_files.sort()
    assert actual_files == [b'README.md', b'docs', b'galaxy.yml', b'playbooks', b'plugins', b'roles']

    # Filter out the progress cursor display calls.
    display_msgs = [m[1][0] for m in mock_display.mock_calls if 'newline' not in m[2]]
    assert len(display_msgs) == 4
    # Msg1 is the warning about not MANIFEST.json, cannot really check message as it has line breaks which varies based
    # on the path size
    assert display_msgs[1] == "Process install dependency map"
    assert display_msgs[2] == "Starting collection install process"
    assert display_msgs[3] == "Skipping 'ansible_namespace.collection' as it is already installed"


# Makes sure we don't get stuck in some recursive loop
@pytest.mark.parametrize('collection_artifact', [
    {'ansible_namespace.collection': '>=0.0.1'},
], indirect=True)
def test_install_collection_with_circular_dependency(collection_artifact, monkeypatch):
    collection_path, collection_tar = collection_artifact
    temp_path = os.path.split(collection_tar)[0]
    shutil.rmtree(collection_path)

    mock_display = MagicMock()
    monkeypatch.setattr(Display, 'display', mock_display)

    collection.install_collections([(to_text(collection_tar), '*', None,)], to_text(temp_path),
                                   [u'https://galaxy.ansible.com'], True, False, False, False, False)

    assert os.path.isdir(collection_path)

    actual_files = os.listdir(collection_path)
    actual_files.sort()
    assert actual_files == [b'FILES.json', b'MANIFEST.json', b'README.md', b'docs', b'playbooks', b'plugins', b'roles']

    with open(os.path.join(collection_path, b'MANIFEST.json'), 'rb') as manifest_obj:
        actual_manifest = json.loads(to_text(manifest_obj.read()))

    assert actual_manifest['collection_info']['namespace'] == 'ansible_namespace'
    assert actual_manifest['collection_info']['name'] == 'collection'
    assert actual_manifest['collection_info']['version'] == '0.1.0'

    # Filter out the progress cursor display calls.
    display_msgs = [m[1][0] for m in mock_display.mock_calls if 'newline' not in m[2]]
    assert len(display_msgs) == 3
    assert display_msgs[0] == "Process install dependency map"
    assert display_msgs[1] == "Starting collection install process"
    assert display_msgs[2] == "Installing 'ansible_namespace.collection:0.1.0' to '%s'" % to_text(collection_path)
