# Copyright (c) 2016 by Kaminario Technologies, Ltd.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Volume driver for Kaminario K2 all-flash arrays."""

import math
import re
import threading

import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import importutils
from oslo_utils import units
from oslo_utils import versionutils
import requests
import six

import cinder
from cinder import exception
from cinder.i18n import _, _LE, _LW, _LI
from cinder import utils
from cinder.volume.drivers.san import san
from cinder.volume import utils as vol_utils

krest = importutils.try_import("krest")

K2_MIN_VERSION = '2.2.0'
K2_LOCK_PREFIX = 'Kaminario'
MAX_K2_RETRY = 5
LOG = logging.getLogger(__name__)

kaminario2_opts = [
    cfg.BoolOpt('auto_calc_max_oversubscription_ratio',
                default=False,
                help="K2 driver will calculate max_oversubscription_ratio "
                     "on setting this option as True."),
    cfg.BoolOpt('suppress_requests_ssl_warnings',
                default=False,
                help="Suppress requests library SSL certificate "
                     "warnings in K2 driver.")]

K2HTTPError = requests.exceptions.HTTPError
K2_RETRY_ERRORS = ("MC_ERR_BUSY", "MC_ERR_BUSY_SPECIFIC",
                   "MC_ERR_INPROGRESS", "MC_ERR_START_TIMEOUT")

if krest:
    class KrestWrap(krest.EndPoint):
        def __init__(self, *args, **kwargs):
            self.krestlock = threading.Lock()
            super(KrestWrap, self).__init__(*args, **kwargs)

        def _should_retry(self, err_code, err_msg):
            if err_code == 400:
                for er in K2_RETRY_ERRORS:
                    if er in err_msg:
                        LOG.debug("Retry ERROR: %d with status %s",
                                  err_code, err_msg)
                        return True
            return False

        @utils.retry(exception.KaminarioRetryableException,
                     retries=MAX_K2_RETRY)
        def _request(self, method, *args, **kwargs):
            try:
                self.krestlock.acquire()
                return super(KrestWrap, self)._request(method,
                                                       *args, **kwargs)
            except K2HTTPError as err:
                err_code = err.response.status_code
                err_msg = err.response.text
                if self._should_retry(err_code, err_msg):
                    raise exception.KaminarioRetryableException(
                        reason=six.text_type(err_msg))
                raise
            finally:
                self.krestlock.release()


def kaminario_logger(func):
    """Return a function wrapper.

    The wrapper adds log for entry and exit to the function.
    """
    def func_wrapper(*args, **kwargs):
        LOG.debug('Entering %(function)s of %(class)s with arguments: '
                  ' %(args)s, %(kwargs)s',
                  {'class': args[0].__class__.__name__,
                   'function': func.__name__,
                   'args': args[1:],
                   'kwargs': kwargs})
        ret = func(*args, **kwargs)
        LOG.debug('Exiting %(function)s of %(class)s '
                  'having return value: %(ret)s',
                  {'class': args[0].__class__.__name__,
                   'function': func.__name__,
                   'ret': ret})
        return ret
    return func_wrapper


class KaminarioCinderDriver(cinder.volume.driver.ISCSIDriver):
    VENDOR = "Kaminario"
    stats = {}

    def __init__(self, *args, **kwargs):
        super(KaminarioCinderDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(san.san_opts)
        self.configuration.append_config_values(kaminario2_opts)
        self._protocol = None
        k2_lock_sfx = self.configuration.safe_get('san_ip')
        self.k2_lock_name = "%s-%s" % (K2_LOCK_PREFIX, k2_lock_sfx)

    @kaminario_logger
    def check_for_setup_error(self):
        if krest is None:
            msg = _("Unable to import 'krest' python module.")
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        else:
            conf = self.configuration
            if conf.suppress_requests_ssl_warnings:
                LOG.info(_LI("Suppressing requests library SSL warnings "
                             "in K2 cinder driver."))
                requests.packages.urllib3.disable_warnings(
                    requests.packages.urllib3.exceptions.
                    InsecureRequestWarning)
                requests.packages.urllib3.disable_warnings(
                    requests.packages.urllib3.exceptions.
                    InsecurePlatformWarning)
            self.client = KrestWrap(conf.san_ip,
                                    conf.san_login,
                                    conf.san_password,
                                    ssl_validate=False)

            v_rs = self.client.search("system/state")
            if hasattr(v_rs, 'hits') and v_rs.total != 0:
                ver = v_rs.hits[0].rest_api_version
                ver_exist = versionutils.convert_version_to_int(ver)
                ver_min = versionutils.convert_version_to_int(K2_MIN_VERSION)
                if ver_exist < ver_min:
                    msg = _("K2 rest api version should be "
                            ">= %s.") % K2_MIN_VERSION
                    LOG.error(msg)
                    raise exception.KaminarioCinderDriverException(reason=msg)

            else:
                msg = _("K2 rest api version search failed.")
                LOG.error(msg)
                raise exception.KaminarioCinderDriverException(reason=msg)

    def _check_ops(self):
        """Ensure that the options we care about are set."""
        required_ops = ['san_ip', 'san_login', 'san_password']
        for attr in required_ops:
            if not getattr(self.configuration, attr, None):
                raise exception.InvalidInput(reason=_('%s is not set.') % attr)

    @kaminario_logger
    def do_setup(self, context):
        super(KaminarioCinderDriver, self).do_setup(context)
        self._check_ops()

    @kaminario_logger
    def create_volume(self, volume):
        """Volume creation in K2 needs a volume group.

        - create a volume group
        - create a volume in the volume group
        """
        vg_name = self.get_volume_group_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        prov_type = self._get_is_dedup(volume.volume_type)
        try:
            LOG.debug("Creating volume group with name: %(name)s, "
                      "quota: unlimited and dedup_support: %(dedup)s",
                      {'name': vg_name, 'dedup': prov_type})

            vg = self.client.new("volume_groups", name=vg_name, quota=0,
                                 is_dedup=prov_type).save()
            LOG.debug("Creating volume with name: %(name)s, size: %(size)s "
                      "GB, volume_group: %(vg)s",
                      {'name': vol_name, 'size': volume.size, 'vg': vg_name})
            self.client.new("volumes", name=vol_name,
                            size=volume.size * units.Mi,
                            volume_group=vg).save()
        except Exception as ex:
            vg_rs = self.client.search("volume_groups", name=vg_name)
            if vg_rs.total != 0:
                LOG.debug("Deleting vg: %s for failed volume in K2.", vg_name)
                vg_rs.hits[0].delete()
            LOG.exception(_LE("Creation of volume %s failed."), vol_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def create_volume_from_snapshot(self, volume, snapshot):
        """Create volume from snapshot.

        - search for snapshot and retention_policy
        - create a view from snapshot and attach view
        - create a volume and attach volume
        - copy data from attached view to attached volume
        - detach volume and view and finally delete view
        """
        snap_name = self.get_snap_name(snapshot.id)
        view_name = self.get_view_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        cview = src_attach_info = dest_attach_info = None
        rpolicy = self.get_policy()
        properties = utils.brick_get_connector_properties()
        LOG.debug("Searching for snapshot: %s in K2.", snap_name)
        snap_rs = self.client.search("snapshots", short_name=snap_name)
        if hasattr(snap_rs, 'hits') and snap_rs.total != 0:
            snap = snap_rs.hits[0]
            LOG.debug("Creating a view: %(view)s from snapshot: %(snap)s",
                      {'view': view_name, 'snap': snap_name})
            try:
                cview = self.client.new("snapshots",
                                        short_name=view_name,
                                        source=snap, retention_policy=rpolicy,
                                        is_exposable=True).save()
            except Exception as ex:
                LOG.exception(_LE("Creating a view: %(view)s from snapshot: "
                                  "%(snap)s failed"), {"view": view_name,
                                                       "snap": snap_name})
                raise exception.KaminarioCinderDriverException(
                    reason=six.text_type(ex.message))

        else:
            msg = _("Snapshot: %s search failed in K2.") % snap_name
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)

        try:
            conn = self.initialize_connection(cview, properties)
            src_attach_info = self._connect_device(conn)
            self.create_volume(volume)
            conn = self.initialize_connection(volume, properties)
            dest_attach_info = self._connect_device(conn)
            vol_utils.copy_volume(src_attach_info['device']['path'],
                                  dest_attach_info['device']['path'],
                                  snapshot.volume.size * units.Ki,
                                  self.configuration.volume_dd_blocksize,
                                  sparse=True)
            self._kaminario_disconnect_volume(src_attach_info,
                                              dest_attach_info)
            self.terminate_connection(volume, properties)
            self.terminate_connection(cview, properties)
            cview.delete()
        except Exception as ex:
            self._kaminario_disconnect_volume(src_attach_info,
                                              dest_attach_info)
            self.terminate_connection(cview, properties)
            self.terminate_connection(volume, properties)
            cview.delete()
            self.delete_volume(volume)
            LOG.exception(_LE("Copy to volume: %(vol)s from view: %(view)s "
                              "failed"), {"vol": vol_name, "view": view_name})
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def create_cloned_volume(self, volume, src_vref):
        """Create a clone from source volume.

        - attach source volume
        - create and attach new volume
        - copy data from attached source volume to attached new volume
        - detach both volumes
        """
        clone_name = self.get_volume_name(volume.id)
        src_name = self.get_volume_name(src_vref.id)
        src_vol = self.client.search("volumes", name=src_name)
        src_map = self.client.search("mappings", volume=src_vol)
        src_attach_info = dest_attach_info = None
        if src_map.total != 0:
            msg = _("K2 driver does not support clone of a attached volume. "
                    "To get this done, create a snapshot from the attached "
                    "volume and then create a volume from the snapshot.")
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        try:
            properties = utils.brick_get_connector_properties()
            conn = self.initialize_connection(src_vref, properties)
            src_attach_info = self._connect_device(conn)
            self.create_volume(volume)
            conn = self.initialize_connection(volume, properties)
            dest_attach_info = self._connect_device(conn)
            vol_utils.copy_volume(src_attach_info['device']['path'],
                                  dest_attach_info['device']['path'],
                                  src_vref.size * units.Ki,
                                  self.configuration.volume_dd_blocksize,
                                  sparse=True)
            self._kaminario_disconnect_volume(src_attach_info,
                                              dest_attach_info)
            self.terminate_connection(volume, properties)
            self.terminate_connection(src_vref, properties)
        except Exception as ex:
            self._kaminario_disconnect_volume(src_attach_info,
                                              dest_attach_info)
            self.terminate_connection(src_vref, properties)
            self.terminate_connection(volume, properties)
            self.delete_volume(volume)
            LOG.exception(_LE("Create a clone: %s failed."), clone_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def delete_volume(self, volume):
        """Volume in K2 exists in a volume group.

        - delete the volume
        - delete the corresponding volume group
        """
        vg_name = self.get_volume_group_name(volume.id)
        vol_name = self.get_volume_name(volume.id)
        try:
            LOG.debug("Searching and deleting volume: %s in K2.", vol_name)
            vol_rs = self.client.search("volumes", name=vol_name)
            if vol_rs.total != 0:
                vol_rs.hits[0].delete()
            LOG.debug("Searching and deleting vg: %s in K2.", vg_name)
            vg_rs = self.client.search("volume_groups", name=vg_name)
            if vg_rs.total != 0:
                vg_rs.hits[0].delete()
        except Exception as ex:
            LOG.exception(_LE("Deletion of volume %s failed."), vol_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    def _check_for_status(self, obj, status):
        while obj.state != status:
            obj.refresh()
            eventlet.sleep(1)

    @kaminario_logger
    def get_volume_stats(self, refresh=False):
        if refresh:
            self.update_volume_stats()
        stats = self.stats
        stats['storage_protocol'] = self._protocol
        stats['driver_version'] = self.VERSION
        stats['vendor_name'] = self.VENDOR
        backend_name = self.configuration.safe_get('volume_backend_name')
        stats['volume_backend_name'] = (backend_name or
                                        self.__class__.__name__)
        return stats

    def create_export(self, context, volume, connector):
        pass

    def ensure_export(self, context, volume):
        pass

    def remove_export(self, context, volume):
        pass

    @kaminario_logger
    def create_snapshot(self, snapshot):
        """Create a snapshot from a volume_group."""
        vg_name = self.get_volume_group_name(snapshot.volume_id)
        snap_name = self.get_snap_name(snapshot.id)
        rpolicy = self.get_policy()
        try:
            LOG.debug("Searching volume_group: %s in K2.", vg_name)
            vg = self.client.search("volume_groups", name=vg_name).hits[0]
            LOG.debug("Creating a snapshot: %(snap)s from vg: %(vg)s",
                      {'snap': snap_name, 'vg': vg_name})
            self.client.new("snapshots", short_name=snap_name,
                            source=vg, retention_policy=rpolicy,
                            is_auto_deleteable=False).save()
        except Exception as ex:
            LOG.exception(_LE("Creation of snapshot: %s failed."), snap_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def delete_snapshot(self, snapshot):
        """Delete a snapshot."""
        snap_name = self.get_snap_name(snapshot.id)
        try:
            LOG.debug("Searching and deleting snapshot: %s in K2.", snap_name)
            snap_rs = self.client.search("snapshots", short_name=snap_name)
            if snap_rs.total != 0:
                snap_rs.hits[0].delete()
        except Exception as ex:
            LOG.exception(_LE("Deletion of snapshot: %s failed."), snap_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    @kaminario_logger
    def extend_volume(self, volume, new_size):
        """Extend volume."""
        vol_name = self.get_volume_name(volume.id)
        try:
            LOG.debug("Searching volume: %s in K2.", vol_name)
            vol = self.client.search("volumes", name=vol_name).hits[0]
            vol.size = new_size * units.Mi
            LOG.debug("Extending volume: %s in K2.", vol_name)
            vol.save()
        except Exception as ex:
            LOG.exception(_LE("Extending volume: %s failed."), vol_name)
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    def update_volume_stats(self):
        conf = self.configuration
        LOG.debug("Searching system capacity in K2.")
        cap = self.client.search("system/capacity").hits[0]
        LOG.debug("Searching total volumes in K2 for updating stats.")
        total_volumes = self.client.search("volumes").total - 1
        provisioned_vol = cap.provisioned_volumes
        if (conf.auto_calc_max_oversubscription_ratio and cap.provisioned
                and (cap.total - cap.free) != 0):
            ratio = provisioned_vol / float(cap.total - cap.free)
        else:
            ratio = conf.max_over_subscription_ratio
        self.stats = {'QoS_support': False,
                      'free_capacity_gb': cap.free / units.Mi,
                      'total_capacity_gb': cap.total / units.Mi,
                      'thin_provisioning_support': True,
                      'sparse_copy_volume': True,
                      'total_volumes': total_volumes,
                      'thick_provisioning_support': False,
                      'provisioned_capacity_gb': provisioned_vol / units.Mi,
                      'max_oversubscription_ratio': ratio,
                      'kaminario:thin_prov_type': 'dedup/nodedup'}

    def get_initiator_host_name(self, connector):
        """Return the initiator host name.

        Valid characters: 0-9, a-z, A-Z, '-', '_'
        All other characters are replaced with '_'.
        Total characters in initiator host name: 32
        """
        return re.sub('[^0-9a-zA-Z-_]', '_', connector.get('host', ''))[:32]

    def get_volume_group_name(self, vid):
        """Return the volume group name."""
        return "cvg-{0}".format(vid)

    def get_volume_name(self, vid):
        """Return the volume name."""
        return "cv-{0}".format(vid)

    def get_snap_name(self, sid):
        """Return the snapshot name."""
        return "cs-{0}".format(sid)

    def get_view_name(self, vid):
        """Return the view name."""
        return "cview-{0}".format(vid)

    @kaminario_logger
    def _delete_host_by_name(self, name):
        """Deleting host by name."""
        host_rs = self.client.search("hosts", name=name)
        if hasattr(host_rs, "hits") and host_rs.total != 0:
            host = host_rs.hits[0]
            host.delete()

    def get_policy(self):
        """Return the retention policy."""
        try:
            LOG.debug("Searching for retention_policy in K2.")
            return self.client.search("retention_policies",
                                      name="Best_Effort_Retention").hits[0]
        except Exception as ex:
            LOG.exception(_LE("Retention policy search failed in K2."))
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))

    def _get_volume_object(self, volume):
        vol_name = self.get_volume_name(volume.id)
        LOG.debug("Searching volume : %s in K2.", vol_name)
        vol_rs = self.client.search("volumes", name=vol_name)
        if not hasattr(vol_rs, 'hits') or vol_rs.total == 0:
            msg = _("Unable to find volume: %s from K2.") % vol_name
            LOG.error(msg)
            raise exception.KaminarioCinderDriverException(reason=msg)
        return vol_rs.hits[0]

    def _get_lun_number(self, vol, host):
        volsnap = None
        LOG.debug("Searching volsnaps in K2.")
        volsnap_rs = self.client.search("volsnaps", snapshot=vol)
        if hasattr(volsnap_rs, 'hits') and volsnap_rs.total != 0:
            volsnap = volsnap_rs.hits[0]

        LOG.debug("Searching mapping of volsnap in K2.")
        map_rs = self.client.search("mappings", volume=volsnap, host=host)
        return map_rs.hits[0].lun

    def initialize_connection(self, volume, connector):
        pass

    @kaminario_logger
    def terminate_connection(self, volume, connector):
        """Terminate connection of volume from host."""
        # Get volume object
        if type(volume).__name__ != 'RestObject':
            vol_name = self.get_volume_name(volume.id)
            LOG.debug("Searching volume: %s in K2.", vol_name)
            volume_rs = self.client.search("volumes", name=vol_name)
            if hasattr(volume_rs, "hits") and volume_rs.total != 0:
                volume = volume_rs.hits[0]
        else:
            vol_name = volume.name

        # Get host object.
        host_name = self.get_initiator_host_name(connector)
        host_rs = self.client.search("hosts", name=host_name)
        if hasattr(host_rs, "hits") and host_rs.total != 0 and volume:
            host = host_rs.hits[0]
            LOG.debug("Searching and deleting mapping of volume: %(name)s to "
                      "host: %(host)s", {'host': host_name, 'name': vol_name})
            map_rs = self.client.search("mappings", volume=volume, host=host)
            if hasattr(map_rs, "hits") and map_rs.total != 0:
                map_rs.hits[0].delete()
            if self.client.search("mappings", host=host).total == 0:
                LOG.debug("Deleting initiator hostname: %s in K2.", host_name)
                host.delete()
        else:
            LOG.warning(_LW("Host: %s not found on K2."), host_name)

    @kaminario_logger
    def k2_initialize_connection(self, volume, connector):
        # Get volume object.
        if type(volume).__name__ != 'RestObject':
            vol = self._get_volume_object(volume)
        else:
            vol = volume
        # Get host object.
        host, host_rs, host_name = self._get_host_object(connector)
        try:
            # Map volume object to host object.
            LOG.debug("Mapping volume: %(vol)s to host: %(host)s",
                      {'host': host_name, 'vol': vol.name})
            mapping = self.client.new("mappings", volume=vol, host=host).save()
        except Exception as ex:
            if host_rs.total == 0:
                self._delete_host_by_name(host_name)
            LOG.exception(_LE("Unable to map volume: %(vol)s to host: "
                              "%(host)s"), {'host': host_name,
                          'vol': vol.name})
            raise exception.KaminarioCinderDriverException(
                reason=six.text_type(ex.message))
        # Get lun number.
        if type(volume).__name__ == 'RestObject':
            return self._get_lun_number(vol, host)
        else:
            return mapping.lun

    def _get_host_object(self, connector):
        pass

    def _get_is_dedup(self, vol_type):
        if vol_type.extra_specs:
            for item in vol_type.extra_specs:
                if (item.get('key') == 'kaminario:thin_prov_type' and
                   item.get('value') == 'nodedup'):
                    return False
        return True

    @kaminario_logger
    def manage_existing(self, volume, existing_ref):
        vol_name = existing_ref['source-name']
        new_name = self.get_volume_name(volume.id)
        vg_new_name = self.get_volume_group_name(volume.id)
        vg_name = None
        is_dedup = self._get_is_dedup(volume.get('volume_type'))
        try:
            LOG.debug("Searching volume: %s in K2.", vol_name)
            vol = self.client.search("volumes", name=vol_name).hits[0]
            vg = vol.volume_group
            vol_map = False
            if self.client.search("mappings", volume=vol).total != 0:
                vol_map = True
            if is_dedup != vg.is_dedup or vol_map:
                raise exception.ManageExistingInvalidReference(
                    existing_ref=existing_ref,
                    reason=_('Manage volume type invalid.'))
            vol.name = new_name
            vg_name = vg.name
            LOG.debug("Manage new volume name: %s", new_name)
            vg.name = vg_new_name
            LOG.debug("Manage volume group name: %s", vg_new_name)
            vg.save()
            LOG.debug("Manage volume: %s in K2.", vol_name)
            vol.save()
        except Exception as ex:
            vg_rs = self.client.search("volume_groups", name=vg_new_name)
            if hasattr(vg_rs, 'hits') and vg_rs.total != 0:
                vg = vg_rs.hits[0]
                if vg_name and vg.name == vg_new_name:
                    vg.name = vg_name
                    LOG.debug("Updating vg new name to old name: %s ", vg_name)
                    vg.save()
            LOG.exception(_LE("manage volume: %s failed."), vol_name)
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=six.text_type(ex.message))
 
    @kaminario_logger
    def manage_existing_get_size(self, volume, existing_ref):
        vol_name = existing_ref['source-name']
        v_rs = self.client.search("volumes", name=vol_name)
        if hasattr(v_rs, 'hits') and v_rs.total != 0:
            vol = v_rs.hits[0]
            size = vol.size / units.Mi
            return math.ceil(size)
        else:
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref,
                reason=_('Unable to get size of manage volume.'))

    @kaminario_logger
    def after_volume_copy(self, ctxt, volume, new_volume, remote=None):
        self.delete_volume(volume)
        vg_name_old = self.get_volume_group_name(volume.id)
        vol_name_old = self.get_volume_name(volume.id)
        vg_name_new = self.get_volume_group_name(new_volume.id)
        vol_name_new = self.get_volume_name(new_volume.id)
        vg_new = self.client.search("volume_groups", name=vg_name_new).hits[0]
        vg_new.name = vg_name_old
        vg_new.save()
        vol_new = self.client.search("volumes", name=vol_name_new).hits[0]
        vol_new.name = vol_name_old
        vol_new.save()
    
    def _kaminario_disconnect_volume(self, *attach_info):
        for info in attach_info:
            if (info and info.get('connector') and
                    info.get('conn', {}).get('data') and info.get('device')):
                info['connector'].disconnect_volume(info['conn']['data'],
                                                    info['device'])

