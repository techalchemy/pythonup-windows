import atexit
import itertools
import pathlib
import shutil
import sys
import tempfile

import click
import requests

from . import configs, metadata, versions


def download_installer(version):
    click.echo('Downloading {}'.format(version.url))
    response = requests.get(version.url, stream=True)
    response.raise_for_status()

    installer_name = version.url.rsplit('/', 1)[-1]
    total = response.headers.get('content-length', '')
    chunks = []

    if total.isdigit():
        length = int(total)
    else:
        length = None
    with click.progressbar(length=length, label=installer_name) as b:
        for chunk in response.iter_content(chunk_size=4096):
            chunks.append(chunk)
            if length is not None:
                b.update(len(chunk))

    data = b''.join(chunks)

    # TODO: Maybe we should cache the installer to a consistent location
    # instead, so we don't need to download again when uninstalling?
    tempdir_path = pathlib.Path(tempfile.mkdtemp())
    atexit.register(shutil.rmtree, str(tempdir_path), ignore_errors=True)

    installer_path = tempdir_path.joinpath(installer_name)
    version.save_installer(data, installer_path)

    return installer_path


def get_version(name):
    force_32 = not metadata.can_install_64bit()
    try:
        version = versions.get_version(name, force_32=force_32)
    except versions.VersionNotFoundError:
        click.echo('No such version: {}'.format(name), err=True)
        sys.exit(1)
    if version.name != name:
        click.echo('Note: Selecting {} instead of {}'.format(
            version.name, name,
        ))
    return version


def check_status(version, expection, *, on_exit=None):
    if version.is_installed() == expection:
        return
    if expection:
        message = '{} is not installed.'
    else:
        message = '{} is already installed.'
    click.echo(message.format(version), err=True)
    if on_exit:
        on_exit()
    sys.exit(1)


def publish_python(version, target, *, overwrite, quiet=False):
    if not overwrite and target.exists():
        return
    if not quiet:
        click.echo('  {}'.format(target.name))
    target.write_text('@py -{} %*'.format(version.name))


def publish_pip(version, target, *, overwrite, quiet=False):
    if not overwrite and target.exists():
        return
    if not quiet:
        click.echo('  {}'.format(target.name))
    target.write_text('@py -{} -m pip %*'.format(version.name))


def publish_script(source, target, *, quiet, overwrite):
    if not overwrite and target.exists():
        return
    if not quiet:
        click.echo('  {}'.format(target.name))
    shutil.copy2(str(source.resolve()), str(target))


def publish_version_scripts(version, target_dir, *, quiet, overwrite=False):
    if not quiet:
        click.echo('Publishing {}...'.format(version))

    target = target_dir.joinpath('python{}.cmd'.format(version.major_version))
    publish_python(version, target, quiet=quiet, overwrite=overwrite)

    try:
        scripts_dir = version.get_scripts_dir_path()
    except FileNotFoundError:
        pass
    else:
        for path in scripts_dir.iterdir():
            if path.stem in ('easy_install', 'pip'):
                # Don't publish versionless pip and easy_install.
                continue
            target = target_dir.joinpath(path.name)
            publish_script(path, target, quiet=quiet, overwrite=overwrite)


def deactivate(*, quiet=False):
    if not quiet:
        click.echo('Removing scripts.')
    for p in configs.get_scripts_dir_path().iterdir():
        p.unlink()


def activate(versions, *, quiet=False):
    scripts_dir = configs.get_scripts_dir_path()
    for version in versions:
        publish_version_scripts(version, scripts_dir, quiet=quiet)
    configs.get_python_versions_path().write_text(
        '\n'.join(version.name for version in versions),
    )


def link_commands(version):
    for path in version.pythons:
        click.echo('Publishing {}'.format(path.name))
        publish_python(version, path, overwrite=True, quiet=True)
    for path in version.pips:
        click.echo('Publishing {}'.format(path.name))
        publish_pip(version, path, overwrite=True, quiet=True)


def unlink_commands(version):
    for p in itertools.chain(version.pythons, version.pips):
        click.echo('Unlinking {}'.format(p.name))
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                click.echo('Failed to remove {} ({})'.format(p, e), err=True)


def get_active_names():
    try:
        content = configs.get_python_versions_path().read_text()
    except FileNotFoundError:
        return ()
    return tuple(v for v in content.split() if v)


def get_versions(installed_only):
    vers = versions.get_versions()
    names = set(v.name for v in vers)

    def should_include(version):
        if installed_only and not version.is_installed():
            return False
        # On a 32-bit host, hide 64-bit names if there is a 32-bit counterpart.
        if (not metadata.can_install_64bit() and
                not version.name.endswith('-32') and
                '{}-32'.format(version.name) in names):
            return False
        return True

    return [v for v in vers if should_include(v)]


def update_active_versions(*, remove=frozenset()):
    current_active_names = set(get_active_names())
    active_names = [n for n in current_active_names]
    for version in remove:
        try:
            active_names.remove(version.name)
        except ValueError:
            continue
        click.echo('Deactivating {}'.format(version))
    if len(current_active_names) != len(active_names):
        deactivate()
        activate([get_version(n) for n in active_names])
