import ConfigParser
import boto.exception
import boto.s3.connection
import bunch
import itertools
import os
import random
import string
import time

from nose.tools import eq_ as eq
from nose.plugins.attrib import attr

from utils import assert_raises

NONEXISTENT_EMAIL = 'doesnotexist@dreamhost.com.invalid'

s3 = bunch.Bunch()
config = bunch.Bunch()

# this will be assigned by setup()
prefix = None


def choose_bucket_prefix(template, max_len=30):
    """
    Choose a prefix for our test buckets, so they're easy to identify.

    Use template and feed it more and more random filler, until it's
    as long as possible but still below max_len.
    """
    rand = ''.join(
        random.choice(string.ascii_lowercase + string.digits)
        for c in range(255)
        )

    while rand:
        s = template.format(random=rand)
        if len(s) <= max_len:
            return s
        rand = rand[:-1]

    raise RuntimeError(
        'Bucket prefix template is impossible to fulfill: {template!r}'.format(
            template=template,
            ),
        )


def nuke_prefixed_buckets():
    for name, conn in s3.items():
        print 'Cleaning buckets from connection {name}'.format(name=name)
        for bucket in conn.get_all_buckets():
            if bucket.name.startswith(prefix):
                print 'Cleaning bucket {bucket}'.format(bucket=bucket)
                try:
                    for key in bucket.list():
                        print 'Cleaning bucket {bucket} key {key}'.format(
                            bucket=bucket,
                            key=key,
                            )
                        key.delete()
                    bucket.delete()
                except boto.exception.S3ResponseError as e:
                    # TODO workaround for buggy rgw that fails to send
                    # error_code, remove
                    if (e.status == 403
                        and e.error_code is None
                        and e.body == ''):
                        e.error_code = 'AccessDenied'
                    if e.error_code != 'AccessDenied':
                        print 'GOT UNWANTED ERROR', e.error_code
                        raise
                    # seems like we're not the owner of the bucket; ignore
                    pass

    print 'Done with cleanup of test buckets.'


def setup():

    cfg = ConfigParser.RawConfigParser()
    try:
        path = os.environ['S3TEST_CONF']
    except KeyError:
        raise RuntimeError(
            'To run tests, point environment '
            + 'variable S3TEST_CONF to a config file.',
            )
    with file(path) as f:
        cfg.readfp(f)

    global prefix
    try:
        template = cfg.get('fixtures', 'bucket prefix')
    except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
        template = 'test-{random}-'
    prefix = choose_bucket_prefix(template=template)

    s3.clear()
    config.clear()
    for section in cfg.sections():
        try:
            (type_, name) = section.split(None, 1)
        except ValueError:
            continue
        if type_ != 's3':
            continue
        try:
            port = cfg.getint(section, 'port')
        except ConfigParser.NoOptionError:
            port = None

        config[name] = bunch.Bunch()
        for var in [
            'user_id',
            'display_name',
            'email',
            ]:
            try:
                config[name][var] = cfg.get(section, var)
            except ConfigParser.NoOptionError:
                pass
        conn = boto.s3.connection.S3Connection(
            aws_access_key_id=cfg.get(section, 'access_key'),
            aws_secret_access_key=cfg.get(section, 'secret_key'),
            is_secure=cfg.getboolean(section, 'is_secure'),
            port=port,
            host=cfg.get(section, 'host'),
            # TODO support & test all variations
            calling_format=boto.s3.connection.OrdinaryCallingFormat(),
            )
        s3[name] = conn

    # WARNING! we actively delete all buckets we see with the prefix
    # we've chosen! Choose your prefix with care, and don't reuse
    # credentials!

    # We also assume nobody else is going to use buckets with that
    # prefix. This is racy but given enough randomness, should not
    # really fail.
    nuke_prefixed_buckets()


def teardown():
    # remove our buckets here also, to avoid littering
    nuke_prefixed_buckets()


bucket_counter = itertools.count(1)


def get_new_bucket(connection=None):
    """
    Get a bucket that exists and is empty.

    Always recreates a bucket from scratch. This is useful to also
    reset ACLs and such.
    """
    if connection is None:
        connection = s3.main
    name = '{prefix}{num}'.format(
        prefix=prefix,
        num=next(bucket_counter),
        )
    # the only way for this to fail with a pre-existing bucket is if
    # someone raced us between setup nuke_prefixed_buckets and here;
    # ignore that as astronomically unlikely
    bucket = connection.create_bucket(name)
    return bucket


def check_access_denied(fn, *args, **kwargs):
    e = assert_raises(boto.exception.S3ResponseError, fn, *args, **kwargs)
    eq(e.status, 403)
    eq(e.reason, 'Forbidden')
    eq(e.error_code, 'AccessDenied')


def test_bucket_list_empty():
    bucket = get_new_bucket()
    l = bucket.list()
    l = list(l)
    eq(l, [])

def test_bucket_notexist():
    name = '{prefix}foo'.format(prefix=prefix)
    print 'Trying bucket {name!r}'.format(name=name)
    e = assert_raises(boto.exception.S3ResponseError, s3.main.get_bucket, name)
    eq(e.status, 404)
    eq(e.reason, 'Not Found')
    eq(e.error_code, 'NoSuchBucket')


def test_bucket_create_delete():
    name = '{prefix}foo'.format(prefix=prefix)
    print 'Trying bucket {name!r}'.format(name=name)
    bucket = s3.main.create_bucket(name)
    # make sure it's actually there
    s3.main.get_bucket(bucket.name)
    bucket.delete()
    # make sure it's gone
    e = assert_raises(boto.exception.S3ResponseError, bucket.delete)
    eq(e.status, 404)
    eq(e.reason, 'Not Found')
    eq(e.error_code, 'NoSuchBucket')


def test_object_read_notexist():
    bucket = get_new_bucket()
    key = bucket.new_key('foobar')
    e = assert_raises(boto.exception.S3ResponseError, key.get_contents_as_string)
    eq(e.status, 404)
    eq(e.reason, 'Not Found')
    eq(e.error_code, 'NoSuchKey')


def test_object_write_then_read():
    bucket = get_new_bucket()
    key = bucket.new_key('foo')
    key.set_contents_from_string('bar')
    got = key.get_contents_as_string()
    eq(got, 'bar')


def check_bad_bucket_name(name):
    e = assert_raises(boto.exception.S3ResponseError, s3.main.create_bucket, name)
    eq(e.status, 400)
    eq(e.reason, 'Bad Request')
    eq(e.error_code, 'InvalidBucketName')


# AWS does not enforce all documented bucket restrictions.
# http://docs.amazonwebservices.com/AmazonS3/2006-03-01/dev/index.html?BucketRestrictions.html
@attr('fails_on_aws')
# TODO rgw fails to provide error_code
# http://tracker.newdream.net/issues/977
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_starts_nonalpha():
    check_bad_bucket_name('_alphasoup')


# TODO this seems to hang until timeout on rgw?
# http://tracker.newdream.net/issues/983
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_short_empty():
    # bucket creates where name is empty look like PUTs to the parent
    # resource (with slash), hence their error response is different
    e = assert_raises(boto.exception.S3ResponseError, s3.main.create_bucket, '')
    eq(e.status, 405)
    eq(e.reason, 'Method Not Allowed')
    eq(e.error_code, 'MethodNotAllowed')


# TODO rgw fails to provide error_code
# http://tracker.newdream.net/issues/977
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_short_one():
    check_bad_bucket_name('a')


# TODO rgw fails to provide error_code
# http://tracker.newdream.net/issues/977
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_short_two():
    check_bad_bucket_name('aa')


# TODO rgw fails to provide error_code
# http://tracker.newdream.net/issues/977
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_long():
    check_bad_bucket_name(256*'a')
    check_bad_bucket_name(280*'a')
    check_bad_bucket_name(3000*'a')


def check_good_bucket_name(name):
    # prefixing to make then unique; tests using this must *not* rely
    # on being able to set the initial character, or exceed the max
    # len
    s3.main.create_bucket('{prefix}{name}'.format(
            prefix=prefix,
            name=name,
            ))


def _test_bucket_create_naming_good_long(length):
    assert len(prefix) < 255
    num = length - len(prefix)
    s3.main.create_bucket('{prefix}{name}'.format(
            prefix=prefix,
            name=num*'a',
            ))


def test_bucket_create_naming_good_long_250():
    _test_bucket_create_naming_good_long(250)


# breaks nuke_prefixed_buckets in teardown, claims a bucket from
# conn.get_all_buckets() suddenly does not exist
# http://tracker.newdream.net/issues/985
@attr('fails_on_rgw')
def test_bucket_create_naming_good_long_251():
    _test_bucket_create_naming_good_long(251)


# breaks nuke_prefixed_buckets in teardown, claims a bucket from
# conn.get_all_buckets() suddenly does not exist
# http://tracker.newdream.net/issues/985
@attr('fails_on_rgw')
def test_bucket_create_naming_good_long_252():
    _test_bucket_create_naming_good_long(252)


# breaks nuke_prefixed_buckets in teardown, claims a bucket from
# conn.get_all_buckets() suddenly does not exist
# http://tracker.newdream.net/issues/985
@attr('fails_on_rgw')
def test_bucket_create_naming_good_long_253():
    _test_bucket_create_naming_good_long(253)


# breaks nuke_prefixed_buckets in teardown, claims a bucket from
# conn.get_all_buckets() suddenly does not exist
# http://tracker.newdream.net/issues/985
@attr('fails_on_rgw')
def test_bucket_create_naming_good_long_254():
    _test_bucket_create_naming_good_long(254)


# TODO breaks nuke_prefixed_buckets in teardown, claims a bucket from
# conn.get_all_buckets() suddenly does not exist
# http://tracker.newdream.net/issues/985
@attr('fails_on_rgw')
def test_bucket_create_naming_good_long_255():
    _test_bucket_create_naming_good_long(255)

# TODO rgw bug makes the list(got) fail with NoSuchKey when length
# >=251
# http://tracker.newdream.net/issues/985
@attr('fails_on_rgw')
def test_bucket_list_long_name():
    length = 251
    num = length - len(prefix)
    bucket = s3.main.create_bucket('{prefix}{name}'.format(
            prefix=prefix,
            name=num*'a',
            ))
    got = bucket.list()
    got = list(got)
    eq(got, [])


# AWS does not enforce all documented bucket restrictions.
# http://docs.amazonwebservices.com/AmazonS3/2006-03-01/dev/index.html?BucketRestrictions.html
@attr('fails_on_aws')
# TODO rgw fails to provide error_code
# http://tracker.newdream.net/issues/977
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_ip():
    check_bad_bucket_name('192.168.5.123')


# TODO rgw fails to provide error_code
# http://tracker.newdream.net/issues/977
@attr('fails_on_rgw')
def test_bucket_create_naming_bad_punctuation():
    # characters other than [a-zA-Z0-9._-]
    check_bad_bucket_name('alpha!soup')


# test_bucket_create_naming_dns_* are valid but not recommended

def test_bucket_create_naming_dns_underscore():
    check_good_bucket_name('foo_bar')


def test_bucket_create_naming_dns_long():
    assert len(prefix) < 50
    num = 100 - len(prefix)
    check_good_bucket_name(num * 'a')


def test_bucket_create_naming_dns_dash_at_end():
    check_good_bucket_name('foo-')


def test_bucket_create_naming_dns_dot_dot():
    check_good_bucket_name('foo..bar')


def test_bucket_create_naming_dns_dot_dash():
    check_good_bucket_name('foo.-bar')


def test_bucket_create_naming_dns_dash_dot():
    check_good_bucket_name('foo-.bar')


def test_bucket_create_exists():
    bucket = get_new_bucket()
    # REST idempotency means this should be a nop
    s3.main.create_bucket(bucket.name)


def test_bucket_create_exists_nonowner():
    # Names are shared across a global namespace. As such, no two
    # users can create a bucket with that same name.
    bucket = get_new_bucket()
    e = assert_raises(boto.exception.S3CreateError, s3.alt.create_bucket, bucket.name)
    eq(e.status, 409)
    eq(e.reason, 'Conflict')
    eq(e.error_code, 'BucketAlreadyExists')


def test_bucket_delete_nonowner():
    bucket = get_new_bucket()
    check_access_denied(s3.alt.delete_bucket, bucket.name)


# TODO radosgw returns the access_key instead of user_id
# http://tracker.newdream.net/issues/980
@attr('fails_on_rgw')
def test_bucket_acl_default():
    bucket = get_new_bucket()
    policy = bucket.get_acl()
    print repr(policy)
    eq(policy.owner.type, None)
    eq(policy.owner.id, config.main.user_id)
    eq(policy.owner.display_name, config.main.display_name)
    eq(len(policy.acl.grants), 1)
    eq(policy.acl.grants[0].permission, 'FULL_CONTROL')
    eq(policy.acl.grants[0].id, policy.owner.id)
    eq(policy.acl.grants[0].display_name, policy.owner.display_name)
    eq(policy.acl.grants[0].uri, None)
    eq(policy.acl.grants[0].email_address, None)
    eq(policy.acl.grants[0].type, 'CanonicalUser')


# TODO rgw bucket.set_acl() gives 403 Forbidden
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_bucket_acl_canned():
    bucket = get_new_bucket()
    # Since it defaults to private, set it public-read first
    bucket.set_acl('public-read')
    policy = bucket.get_acl()
    print repr(policy)
    eq(len(policy.acl.grants), 2)
    eq(policy.acl.grants[0].permission, 'FULL_CONTROL')
    eq(policy.acl.grants[0].id, policy.owner.id)
    eq(policy.acl.grants[0].display_name, policy.owner.display_name)
    eq(policy.acl.grants[0].uri, None)
    eq(policy.acl.grants[0].email_address, None)
    eq(policy.acl.grants[0].type, 'CanonicalUser')
    eq(policy.acl.grants[1].permission, 'READ')
    eq(policy.acl.grants[1].id, None)
    eq(policy.acl.grants[1].display_name, None)
    eq(policy.acl.grants[1].uri, 'http://acs.amazonaws.com/groups/global/AllUsers')
    eq(policy.acl.grants[1].email_address, None)
    eq(policy.acl.grants[1].type, 'Group')

    # Then back to private.
    bucket.set_acl('private')
    policy = bucket.get_acl()
    print repr(policy)
    eq(len(policy.acl.grants), 1)
    eq(policy.acl.grants[0].permission, 'FULL_CONTROL')
    eq(policy.acl.grants[0].id, policy.owner.id)
    eq(policy.acl.grants[0].display_name, policy.owner.display_name)
    eq(policy.acl.grants[0].uri, None)
    eq(policy.acl.grants[0].email_address, None)
    eq(policy.acl.grants[0].type, 'CanonicalUser')


# TODO rgw bucket.set_acl() gives 403 Forbidden
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_bucket_acl_canned_private_to_private():
    bucket = get_new_bucket()
    bucket.set_acl('private')


# TODO rgw bucket.set_acl() gives 403 Forbidden
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_bucket_acl_grant_userid():
    bucket = get_new_bucket()
    # add alt user
    policy = bucket.get_acl()
    policy.acl.add_user_grant('FULL_CONTROL', config.alt.user_id)
    bucket.set_acl(policy)
    policy = bucket.get_acl()
    eq(len(policy.acl.grants), 2)
    eq(policy.acl.grants[1].permission, 'FULL_CONTROL')
    eq(policy.acl.grants[1].id, config.alt.user_id)
    eq(policy.acl.grants[1].display_name, config.alt.display_name)
    eq(policy.acl.grants[1].uri, None)
    eq(policy.acl.grants[1].email_address, None)
    eq(policy.acl.grants[1].type, 'CanonicalUser')

    # alt user can write
    bucket2 = s3.alt.get_bucket(bucket.name)
    key = bucket2.new_key('foo')
    key.set_contents_from_string('bar')


# TODO rgw bucket.set_acl() gives 403 Forbidden
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_bucket_acl_grant_email():
    bucket = get_new_bucket()
    # add alt user
    policy = bucket.get_acl()
    policy.acl.add_email_grant('FULL_CONTROL', config.alt.email)
    bucket.set_acl(policy)
    policy = bucket.get_acl()
    eq(len(policy.acl.grants), 2)
    eq(policy.acl.grants[1].permission, 'FULL_CONTROL')
    eq(policy.acl.grants[1].id, config.alt.user_id)
    eq(policy.acl.grants[1].display_name, config.alt.display_name)
    eq(policy.acl.grants[1].uri, None)
    eq(policy.acl.grants[1].email_address, None)
    eq(policy.acl.grants[1].type, 'CanonicalUser')

    # alt user can write
    bucket2 = s3.alt.get_bucket(bucket.name)
    key = bucket2.new_key('foo')
    key.set_contents_from_string('bar')


# TODO rgw gives 403 error
# http://tracker.newdream.net/issues/982
@attr('fails_on_rgw')
def test_bucket_acl_grant_email_notexist():
    # behavior not documented by amazon
    bucket = get_new_bucket()
    policy = bucket.get_acl()
    policy.acl.add_email_grant('FULL_CONTROL', NONEXISTENT_EMAIL)
    e = assert_raises(boto.exception.S3ResponseError, bucket.set_acl, policy)
    eq(e.status, 400)
    eq(e.reason, 'Bad Request')
    eq(e.error_code, 'UnresolvableGrantByEmailAddress')


# TODO rgw bucket.set_acl() gives 403 Forbidden
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_bucket_acl_revoke_all():
    # revoke all access, including the owner's access
    bucket = get_new_bucket()
    policy = bucket.get_acl()
    policy.acl.grants = []
    bucket.set_acl(policy)
    policy = bucket.get_acl()
    eq(len(policy.acl.grants), 0)


# TODO rgw log_bucket.set_as_logging_target() gives 403 Forbidden
# http://tracker.newdream.net/issues/984
@attr('fails_on_rgw')
def test_logging_toggle():
    bucket = get_new_bucket()
    log_bucket = s3.main.create_bucket(bucket.name + '-log')
    log_bucket.set_as_logging_target()
    bucket.enable_logging(target_bucket=log_bucket, target_prefix=bucket.name)
    bucket.disable_logging()


def _setup_access(bucket_acl, object_acl):
    """
    Simple test fixture: create a bucket with given ACL, with objects:

    - a: given ACL
    - b: default ACL
    """
    obj = bunch.Bunch()
    bucket = get_new_bucket()
    bucket.set_acl(bucket_acl)
    obj.a = bucket.new_key('foo')
    obj.a.set_contents_from_string('foocontent')
    obj.a.set_acl(object_acl)
    obj.b = bucket.new_key('bar')
    obj.b.set_contents_from_string('barcontent')

    obj.bucket2 = s3.alt.get_bucket(bucket.name, validate=False)
    obj.a2 = obj.bucket2.new_key(obj.a.name)
    obj.b2 = obj.bucket2.new_key(obj.b.name)
    obj.new = obj.bucket2.new_key('new')

    return obj


def get_bucket_key_names(bucket):
    return frozenset(k.name for k in bucket.list())


# TODO bucket.set_acl('private') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_private_object_private():
    # all the test_access_* tests follow this template
    obj = _setup_access(bucket_acl='private', object_acl='private')
    # acled object read fail
    check_access_denied(obj.a2.get_contents_as_string)
    # acled object write fail
    check_access_denied(obj.a2.set_contents_from_string, 'barcontent')
    # default object read fail
    check_access_denied(obj.b2.get_contents_as_string)
    # default object write fail
    check_access_denied(obj.b2.set_contents_from_string, 'baroverwrite')
    # bucket read fail
    check_access_denied(get_bucket_key_names, obj.bucket2)
    # bucket write fail
    check_access_denied(obj.new.set_contents_from_string, 'newcontent')


# TODO bucket.set_acl('private') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_private_object_publicread():
    obj = _setup_access(bucket_acl='private', object_acl='public-read')
    eq(obj.a2.get_contents_as_string(), 'foocontent')
    check_access_denied(obj.a2.set_contents_from_string, 'foooverwrite')
    check_access_denied(obj.b2.get_contents_as_string)
    check_access_denied(obj.b2.set_contents_from_string, 'baroverwrite')
    check_access_denied(get_bucket_key_names, obj.bucket2)
    check_access_denied(obj.new.set_contents_from_string, 'newcontent')


# TODO bucket.set_acl('private') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_private_object_publicreadwrite():
    obj = _setup_access(bucket_acl='private', object_acl='public-read-write')
    eq(obj.a2.get_contents_as_string(), 'foocontent')
    ### TODO: it seems AWS denies this write, even when we expected it
    ### to complete; as it is unclear what the actual desired behavior
    ### is (the docs are somewhat unclear), we'll just codify current
    ### AWS behavior, at least for now.
    # obj.a2.set_contents_from_string('foooverwrite')
    check_access_denied(obj.a2.set_contents_from_string, 'foooverwrite')
    check_access_denied(obj.b2.get_contents_as_string)
    check_access_denied(obj.b2.set_contents_from_string, 'baroverwrite')
    check_access_denied(get_bucket_key_names, obj.bucket2)
    check_access_denied(obj.new.set_contents_from_string, 'newcontent')


# TODO bucket.set_acl('public-read') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_publicread_object_private():
    obj = _setup_access(bucket_acl='public-read', object_acl='private')
    check_access_denied(obj.a2.get_contents_as_string)
    check_access_denied(obj.a2.set_contents_from_string, 'barcontent')
    ### TODO: i don't understand why this gets denied, but codifying what
    ### AWS does
    # eq(obj.b2.get_contents_as_string(), 'barcontent')
    check_access_denied(obj.b2.get_contents_as_string)
    check_access_denied(obj.b2.set_contents_from_string, 'baroverwrite')
    eq(get_bucket_key_names(obj.bucket2), frozenset(['foo', 'bar']))
    check_access_denied(obj.new.set_contents_from_string, 'newcontent')


# TODO bucket.set_acl('public-read') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_publicread_object_publicread():
    obj = _setup_access(bucket_acl='public-read', object_acl='public-read')
    eq(obj.a2.get_contents_as_string(), 'foocontent')
    check_access_denied(obj.a2.set_contents_from_string, 'foooverwrite')
    ### TODO: i don't understand why this gets denied, but codifying what
    ### AWS does
    # eq(obj.b2.get_contents_as_string(), 'barcontent')
    check_access_denied(obj.b2.get_contents_as_string)
    check_access_denied(obj.b2.set_contents_from_string, 'baroverwrite')
    eq(get_bucket_key_names(obj.bucket2), frozenset(['foo', 'bar']))
    check_access_denied(obj.new.set_contents_from_string, 'newcontent')


# TODO bucket.set_acl('public-read') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_publicread_object_publicreadwrite():
    obj = _setup_access(bucket_acl='public-read', object_acl='public-read-write')
    eq(obj.a2.get_contents_as_string(), 'foocontent')
    ### TODO: it seems AWS denies this write, even when we expected it
    ### to complete; as it is unclear what the actual desired behavior
    ### is (the docs are somewhat unclear), we'll just codify current
    ### AWS behavior, at least for now.
    # obj.a2.set_contents_from_string('foooverwrite')
    check_access_denied(obj.a2.set_contents_from_string, 'foooverwrite')
    ### TODO: i don't understand why this gets denied, but codifying what
    ### AWS does
    # eq(obj.b2.get_contents_as_string(), 'barcontent')
    check_access_denied(obj.b2.get_contents_as_string)
    check_access_denied(obj.b2.set_contents_from_string, 'baroverwrite')
    eq(get_bucket_key_names(obj.bucket2), frozenset(['foo', 'bar']))
    check_access_denied(obj.new.set_contents_from_string, 'newcontent')


# TODO bucket.set_acl('public-read-write') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_publicreadwrite_object_private():
    obj = _setup_access(bucket_acl='public-read-write', object_acl='private')
    check_access_denied(obj.a2.get_contents_as_string)
    obj.a2.set_contents_from_string('barcontent')
    ### TODO: i don't understand why this gets denied, but codifying what
    ### AWS does
    # eq(obj.b2.get_contents_as_string(), 'barcontent')
    check_access_denied(obj.b2.get_contents_as_string)
    obj.b2.set_contents_from_string('baroverwrite')
    eq(get_bucket_key_names(obj.bucket2), frozenset(['foo', 'bar']))
    obj.new.set_contents_from_string('newcontent')


# TODO bucket.set_acl('public-read-write') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_publicreadwrite_object_publicread():
    obj = _setup_access(bucket_acl='public-read-write', object_acl='public-read')
    eq(obj.a2.get_contents_as_string(), 'foocontent')
    obj.a2.set_contents_from_string('barcontent')
    ### TODO: i don't understand why this gets denied, but codifying what
    ### AWS does
    # eq(obj.b2.get_contents_as_string(), 'barcontent')
    check_access_denied(obj.b2.get_contents_as_string)
    obj.b2.set_contents_from_string('baroverwrite')
    eq(get_bucket_key_names(obj.bucket2), frozenset(['foo', 'bar']))
    obj.new.set_contents_from_string('newcontent')


# TODO bucket.set_acl('public-read-write') fails on rgw
# http://tracker.newdream.net/issues/981
@attr('fails_on_rgw')
def test_access_bucket_publicreadwrite_object_publicreadwrite():
    obj = _setup_access(bucket_acl='public-read-write', object_acl='public-read-write')
    eq(obj.a2.get_contents_as_string(), 'foocontent')
    obj.a2.set_contents_from_string('foooverwrite')
    ### TODO: i don't understand why this gets denied, but codifying what
    ### AWS does
    # eq(obj.b2.get_contents_as_string(), 'barcontent')
    check_access_denied(obj.b2.get_contents_as_string)
    obj.b2.set_contents_from_string('baroverwrite')
    eq(get_bucket_key_names(obj.bucket2), frozenset(['foo', 'bar']))
    obj.new.set_contents_from_string('newcontent')