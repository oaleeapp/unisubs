# Amara, universalsubtitles.org
#
# Copyright (C) 2014 Participatory Culture Foundation
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see
# http://www.gnu.org/licenses/agpl-3.0.html.

"""staticmedia.bundles -- bundle media files

This module handles bundling Javascript, CSS, and other media files.  Bundling
the files does several things.

    - Combines multiple files into a single file
    - Compresses/minifies them
    - Optionally processes them through a preprocessor like SASS

See the bundle_* functions for exactly what we do for various media types.
"""

import os
import subprocess
import time

from django.conf import settings
from django.core.cache import cache

def static_root():
    return settings.STATIC_ROOT

def _run_command(commandline, stdin=None):
    p = subprocess.Popen(commandline, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    stdout, stderr = p.communicate(stdin)
    if stderr:
        raise ValueError("Got error from %s: %s" % (commandline, stderr))
    elif p.returncode != 0:
        raise ValueError("Got error code from %s: %s" % (commandline,
                                                         p.returncode))
    else:
        return stdout

class Bundle(object):
    """Represents a single media bundle."""

    mime_type = NotImplemented

    def __init__(self, name, config):
        self.name = name
        self.config = config

    def paths(self):
        root_dir = static_root()
        return [os.path.join(root_dir, p) for p in self.config['files']]

    def concatinate_files(self):
        return ''.join(open(p).read() for p in self.paths())

    def build_contents(self):
        """Build the contents of this bundle

        Subclasses of Bundle must implement this function

        :returns: string representing the bundle
        """
        raise NotImplementedError()

    def modified_since(self, since):
        """Check if any of our files has been modified after a certain time
        """
        return max(os.path.getmtime(p) for p in self.paths()) > since

    def cache_key(self):
        return 'staticmedia:bundle:%s' % self.name

    def get_contents(self):
        """Get the data for this bundle.

        The first time this method is called, we will build the bundle, then
        store the result in the django cache.

        On subsequent calls, we will only build the bundle again if one of our
        files has been modified since the last build.
        """
        cached_value = cache.get(self.cache_key())
        if cached_value is not None:
            if not self.modified_since(cached_value[0]):
                return cached_value[1]
        cache_time = time.time()
        rv = self.build_contents()
        cache.set(self.cache_key(), (cache_time, rv))
        return rv

class JavascriptBundle(Bundle):
    """Bundle Javascript files.

    Javascript files are concatinated together, then run through uglifyjs to
    minify them.
    """

    mime_type = 'text/javascript'

    def build_contents(self):
        return _run_command(['uglifyjs'], stdin=self.concatinate_files())

class CSSBundle(Bundle):
    """Bundle CSS files

    For CSS files, we:
        - Concatinate all files together
        - Use SASS for process them.  We also use SASS to compress the CSS
        files.

    For regular CSS files, SASS simple handles compressing them.  CSS files
    can also use the Sassy CSS format.  SASS is run with --load-path
    STATIC_ROOT/css to control how sass finds modules.
    """

    mime_type = 'text/css'

    def build_contents(self):
        return _run_command([
            'sass', '-t', 'compressed',
            '--load-path', os.path.join(static_root(), 'css'),
            '--scss', '--stdin',
        ], stdin=self.concatinate_files())

_type_to_bundle_class = {
    'js': JavascriptBundle,
    'css': CSSBundle,
}
def get_bundle(name):
    basename, type_ = name.rsplit('.', 1)
    try:
        BundleClass = _type_to_bundle_class[type_]
    except KeyError:
        raise ValueError("Unknown bundle type for %s" % name)
    try:
        config = settings.MEDIA_BUNDLES[name]
    except KeyError:
        # hack to find the setting using the old unisubs_compressor format
        config = settings.MEDIA_BUNDLES[basename]
    return BundleClass(name, config)