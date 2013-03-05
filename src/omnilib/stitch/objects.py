#!/usr/bin/env python

#----------------------------------------------------------------------
# Copyright (c) 2013 Raytheon BBN Technologies
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and/or hardware specification (the "Work") to
# deal in the Work without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Work, and to permit persons to whom the Work
# is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Work.
#
# THE WORK IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE WORK OR THE USE OR OTHER DEALINGS
# IN THE WORK.
#----------------------------------------------------------------------
'''Objects representing RSpecs, Aggregates, Hops, Paths. Includes the main workhorse
functions for doing allocationg and deletions at aggregates.'''

import copy
import json
import logging
import os
import random
import string
import time
from xml.dom.minidom import parseString, Node as XMLNode

from GENIObject import *
from VLANRange import *
import RSpecParser
from utils import *

import omni
from omnilib.util.handler_utils import _construct_output_filename, _writeRSpec, _getRSpecOutput, _printResults
from omnilib.util.dossl import is_busy_reply
from omnilib.util.omnierror import OmniError, AMAPIError

from geni.util import rspec_schema, rspec_util

# FIXME: As in RSpecParser, check use of getAttribute vs getAttributeNS and localName vs nodeName
# FIXME: Merge RSpec element/attribute name constants into RSpecParser

class Path(GENIObject):
    '''Path in stitching aka a Link'''
    __ID__ = validateText

    # XML tag constants
    ID_TAG = 'id'
    HOP_TAG = 'hop'

    @classmethod
    def fromDOM(cls, element):
        """Parse a stitching path from a DOM element."""
        # FIXME: Do we need getAttributeNS?
        id = element.getAttribute(cls.ID_TAG)
        path = Path(id)
        for child in element.childNodes:
            if child.localName == cls.HOP_TAG:
                hop = Hop.fromDOM(child)
                hop.path = path
                hop.idx = len(path.hops)
                path.hops.append(hop)
        for hop in path.hops:
            next_hop = path.find_hop(hop._next_hop)
            if next_hop:
                hop._next_hop = next_hop
        return path

    def __init__(self, id):
        super(Path, self).__init__()
        self.id = id
        self._hops = []
        self._aggregates = set()

    @property
    def hops(self):
        return self._hops

    @property
    def aggregates(self):
        return self._aggregates

    @hops.setter
    def hops(self, hopList):
        self._setListProp('hops', hopList, Hop)

    def find_hop(self, hop_urn):
        for hop in self.hops:
            if hop.urn == hop_urn:
                return hop
        # Fail -- no hop matched the given URN
        return None

    def find_hop_idx(self, hop_idx):
        '''Find a hop in this path by its index, or None'''
        for hop in self.hops:
            if hop.idx == hop_idx:
                return hop
        # Fail -- no hop matched the given index
        return None

    def editChangesIntoDom(self, pathDomNode):
        '''Edit any changes made in this element into the given DomNode'''
        # Note the parent RSpec element's dom is not touched, unless the given node is from that document
        # Here we just find all the Hops and let them do stuff

        # Incoming node should be the node for this path
        nodeId = pathDomNode.getAttribute(self.ID_TAG)
        if nodeId != self.id:
            raise StitchingError("Path %s given Dom node with different Id: %s" % (self, nodeId))

        # For each of this path's hops, find the appropriate Dom element, and let Hop edit itself in
        domHops = pathDomNode.getElementsByTagName(self.HOP_TAG)
        for hop in self.hops:
            domHopNode = None
            if domHops:
                for hopNode in domHops:
                    hopNodeId = hopNode.getAttribute(self.ID_TAG)
                    if hopNodeId == hop._id:
                        domHopNode = hopNode
                        break
            if domHopNode is None:
                # Couldn't find this Hop in the dom
                # FIXME: Create it?
                raise StitchingError("Couldn't find Hop %s in given Dom node to edit in changes" % hop)
            hop.editChangesIntoDom(domHopNode)
        # End of loop over hops
        return

class Stitching(GENIObject):
    __simpleProps__ = [ ['last_update_time', str] ] #, ['path', Path[]]]

    def __init__(self, last_update_time=None, paths=None):
        super(Stitching, self).__init__()
        self.last_update_time = str(last_update_time)
        self.paths = paths

    # Arg of link_id: this is the client_id of the main body link, or the path ID
    def find_path(self, link_id):
        if self.paths:
            for path in self.paths:
                if path.id == link_id:
                    return path
        else:
            return None

class Aggregate(object):
    '''Aggregate'''

    # Hold all instances. One instance per URN.
    aggs = dict()

    # FIXME: Move these constants up higher
    MAX_TRIES = 10 # Max times to try allocating here. Compare with allocateTries
    BUSY_MAX_TRIES = 5 # dossl does 3
    BUSY_POLL_INTERVAL_SEC = 10 # dossl does 10
    SLIVERSTATUS_MAX_TRIES = 10
    SLIVERSTATUS_POLL_INTERVAL_SEC = 10
    PAUSE_FOR_AM_TO_FREE_RESOURCES_SECS = 30

    # Constant name of SCS expanded request (for use here and elsewhere)
    FAKEMODESCSFILENAME = '/tmp/stitching-scs-expanded-request.xml'

    # Directory to store request rspecs - must be universally writable
    REQ_RSPEC_DIR = '/tmp'

    @classmethod
    def find(cls, urn):
        if not urn in cls.aggs:
            m = cls(urn)
            cls.aggs[urn] = m
        return cls.aggs[urn]

    @classmethod
    def all_aggregates(cls):
        return cls.aggs.values()

    @classmethod
    def clearCache(cls):
        cls.aggs = dict()

    def __init__(self, urn, url=None):
        self.urn = urn
        self.url = url
        self.inProcess = False
        self.completed = False
        self.userRequested = False
        self._hops = set()
        self._paths = set()
        self._dependsOn = set() # of Aggregate objects
        self.rspecfileName = None
        self.isDependencyFor = set() # AMs that depend on this: for ripple down deletes
        self.logger = logging.getLogger('stitch.Aggregate')
        # Note these are sort of RSpecs but not RSpec objects, to avoid a loop
        self.requestDom = None # the DOM as constructed to submit in request to this AM
        self.manifestDom = None # the DOM as we got back from the AM
        self.api_version = 2 # Set from stitchhandler.parseSCSResponse
        self.dcn = False # DCN AMs require waiting for sliverstatus to say ready before the manifest is legit
        # reservation tries since last call to SCS
        self.allocateTries = 0 # see MAX_TRIES

        self.pgLogUrl = None # For PG AMs, any log url returned by Omni that we could capture

    def __str__(self):
        return "<Aggregate %s>" % (self.urn)

    def __repr__(self):
        return "Aggregate(%r)" % (self.urn)

    @property
    def hops(self):
        return list(self._hops)

    @property
    def paths(self):
        return list(self._paths)

    @property
    def dependsOn(self):
        return list(self._dependsOn)

    def add_hop(self, hop):
        self._hops.add(hop)

    def add_path(self, path):
        self._paths.add(path)

    def add_dependency(self, agg):
        self._dependsOn.add(agg)

    def add_agg_that_dependsOnThis(self, agg):
        self.isDependencyFor.add(agg)

    @property
    def dependencies_complete(self):
        """Dependencies are complete if there are no dependencies
        or if all dependencies are completed.
        """
        return (not self._dependsOn
                or reduce(lambda a, b: a and b,
                          [agg.completed for agg in self._dependsOn]))

    @property
    def ready(self):
        return not self.completed and not self.inProcess and self.dependencies_complete

    def allocate(self, opts, slicename, rspecDom, scsCallCount):
        '''Main workhorse function. Build the request rspec for this AM,
        and make the reservation. On error, delete and signal failure.'''

        if self.inProcess:
            self.logger.warn("Called allocate on AM already in process: %s", self)
            return
        # Confirm all dependencies still done
        if not self.dependencies_complete:
            self.logger.warn("Cannot allocate at %s: dependencies not ready", self)
            return
        if self.completed:
            self.logger.warn("Called allocate on AM already maked complete", self)
            return

        # FIXME: If we are quitting, return (important when threaded)

        # Import VLANs, noting if we need to delete an old reservation at this AM first
        mustDelete, alreadyDone = self.copyVLANsAndDetectRedo()

        if mustDelete:
            self.logger.info("Must delete previous reservation for %s", self)
            alreadyDone = False
            self.deleteReservation(opts, slicename)

            # FIXME: Need to sleep so AM has time to put those resources back in the pool
            # But really should do this on the AMs own thread to avoid blocking everything else
            time.sleep(self.PAUSE_FOR_AM_TO_FREE_RESOURCES_SECS)
        # end of block to delete a previous reservation

        if alreadyDone:
            # we did a previous upstream delete and worked our way down to here, but this AM is OK
            self.completed = True
            self.logger.info("%s had previous result we didn't need to redo. Done", self)
            return

        # Check that all hops have reasonable vlan inputs
        for hop in self.hops:
            if not hop._hop_link.vlan_suggested_request <= hop._hop_link.vlan_range_request:
                raise StitchingError("%s hop %s suggested %s not in avail %s", self, hop, hop._hop_link.vlan_suggested_request, hop._hop_link.vlan_range_request)

        if self.allocateTries == self.MAX_TRIES:
            self.logger.warn("Doing allocate on %s for %dth time!", self, self.allocateTries)

        self.completed = False

        # Mark AM is busy
        self.inProcess = True

        # Generate the new request Dom
        self.requestDom = self.getEditedRSpecDom(rspecDom)

        # Get the manifest for this AM
        # result is a manifest RSpec string. Errors wouuld be raised
        # This method handles fakeMode, retrying on BUSY, polling SliverStatus for DCN AMs,
        # VLAN_UNAVAILABLE errors, other errors
        manifestString = self.doReservation(opts, slicename, scsCallCount)

        # Save it on the Agg
        try:
            self.manifestDom = parseString(manifestString)

            # FIXME: Do this? We get the same info on the combined manifest already
            # Put the AM reservation info in a comment on the per AM manifest
#            commentText = "AM %s at %s reservation using APIv%d. " % (self.urn, self.url, self.api_version)
#            if self.pgLogUrl:
#                commentText = commentText + "PG Log URL: %s" % self.pgLogUrl
#            logComment = self.manifestDom.createComment(commentText)
#            first_non_comment_element = None
#            for elt in dom_self.manifestDom.childNodes:
#                if elt.nodeType != Node.COMMENT_NODE:
#                    first_non_comment_element = elt;
#                    break
#                self.manifestDom.insertBefore(comment_element, first_non_comment_element)
        except Exception, e:
            self.logger.error("Failed to parse %s reservation result as DOM XML RSpec: %s", self, e)
            raise StitchingError("%s manifest rspec not parsable: %s" % (self, e))

        hadSuggestedNotRequest = False

        # Parse out the VLANs we got, saving them away on the HopLinks
        # Note and complain if we didn't get VLANs or the VLAN we got is not what we requested
        for hop in self.hops:
            range_suggested = self.getVLANRangeSuggested(self.manifestDom, hop._id, hop.path.id)
            rangeValue = range_suggested[0]
            suggestedValue = range_suggested[1]
            if not suggestedValue:
                self.logger.error("Didn't find suggested value in rspec for hop %s", hop)
                # Treat as error? Or as vlan unavailable? FIXME
                self.handleVlanUnavailable("reservation", ("No suggested value element on hop %s" % hop), hop, True)
            elif suggestedValue in ('null', 'None', 'any'):
                self.logger.error("Hop %s Suggested invalid: %s", hop, suggestedValue)
                # Treat as error? Or as vlan unavailable? FIXME
                self.handleVlanUnavailable("reservation", ("Invalid suggested value %s on hop %s" % (suggestedValue, hop)), hop, True)
            else:
                suggestedObject = VLANRange.fromString(suggestedValue)
            # If these fail and others worked, this is malformed
            if not rangeValue:
                self.logger.error("Didn't find vlanAvailRange element for hop %s", hop)
                raise StitchingError("%s didn't have a vlanAvailRange in manifest" % hop)
            elif rangeValue in ('null', 'None', 'any'):
                self.logger.error("Hop %s availRange invalid: %s", hop, rangeValue)
                raise StitchingError("%s had invalid availVlanRange in manifest: %s" % (hop, rangeValue))
            else:
                rangeObject = VLANRange.fromString(rangeValue)

            # If got here the manifest values are OK - save them away
            self.logger.debug("Hop %s manifest had suggested %s, avail %s", hop, suggestedValue, rangeValue)
            hop._hop_link.vlan_suggested_manifest = suggestedObject
            hop._hop_link.vlan_range_manifest = rangeObject

            if not suggestedObject <= hop._hop_link.vlan_suggested_request:
                self.logger.error("%s gave VLAN %s for hop %s which is not in our request %s", self, suggestedObject, hop, hop._hop_link.vlan_suggested_request)
                # This is sug != requested case
                self.handleSuggestedVLANNotRequest(opts, slicename)
                hadSuggestedNotRequest = True

        # Mark AM not busy
        self.inProcess = False

        self.logger.info("Allocation at %s complete.", self)

        if not hadSuggestedNotRequest:
            # mark self complete
            self.completed = True

    def copyVLANsAndDetectRedo(self):
        '''Copy VLANs to this AMs hops from previous manifests. Check if we already had manifests.
        If so, but the inputs are incompatible, then mark this to be deleted. If so, but the
        inputs are compatible, then an AM upstream was redone, but this is alreadydone.'''

        hadPreviousManifest = self.manifestDom != None
        mustDelete = False # Do we have old reservation to delete?
        alreadyDone = hadPreviousManifest # Did we already complete this AM? (and this is just a recheck)
        for hop in self.hops:
            if not hop.import_vlans:
                if not hop._hop_link.vlan_suggested_manifest:
                    alreadyDone = False
                continue

            # Calculate the new suggested/avail for this hop
            if not hop.import_vlans_from:
                self.logger.warn("%s imports vlans but has no import from?", hop)
                continue

            new_suggested = hop._hop_link.vlan_suggested_request or VLANRange.fromString("any")
            if hop.import_vlans_from._hop_link.vlan_suggested_manifest:
                new_suggested = hop.import_vlans_from._hop_link.vlan_suggested_manifest.copy()
            else:
                self.logger.warn("%s's import_from %s had no suggestedVLAN manifest", hop, hop.import_vlans_from)
                raise StitchingError("%s's import_from %s had no suggestedVLAN manifest" % (hop, hop.import_vlans_from))

            # If we've noted VLANs we already tried that failed (cause of later failures
            # or cause the AM wouldn't give the tag), then be sure to exclude those
            # from new_suggested - that is, if new_suggested would be in that set, then we have
            # an error - gracefully exit, either to SCS excluding this hop or to user
            if new_suggested <= hop.vlans_unavailable:
                # FIXME: use handleVlanUnavailable? Is that right?
                self.handleVlanUnavailable("reserve", "Calculated new_suggested for %s of %s is in set of VLANs we know won't work" % (hop, new_suggested))
#                raise StitchingError("%s picked new_suggested %s that is in the set of VLANs that we know won't work: %s" % (hop, new_suggested, hop.vlans_unavailable))

            int1 = VLANRange.fromString("any")
            int2 = VLANRange.fromString("any")
            if hop.import_vlans_from._hop_link.vlan_range_manifest:
                int1 = hop.import_vlans_from._hop_link.vlan_range_manifest
            else:
                self.logger.warn("%s's import_from %s had no avail VLAN manifest", hop, hop.import_vlans_from)
            if hop._hop_link.vlan_range_request:
                int2 = hop._hop_link.vlan_range_request
            else:
                self.logger.warn("%s had no avail VLAN request", hop, hop.import_vlans_from)
            new_avail = int1 & int2

            # If we've noted VLANs we already tried that failed (cause of later failures
            # or cause the AM wouldn't give the tag), then be sure to exclude those
            # from new_avail. And if new_avail is now empty, that is
            # an error - gracefully exit, either to SCS excluding this hop or to user
            new_avail2 = new_avail - hop.vlans_unavailable
            if new_avail2 != new_avail:
                self.logger.debug("%s computed vlanRange %s smaller due to excluding known unavailable VLANs. Was otherwise %s", hop, new_avail2, new_avail)
                new_avail = new_avail2
            if len(new_avail) == 0:
                # FIXME: Do I go to SCS? treat as VLAN Unavailable? I don't think this should happen.
                # But if it does, it probably means I need to exclude this AM at least?
                self.logger.error("%s computed availVlanRange is empty" % hop)
                raise StitchingError("%s computed availVlanRange is empty" % hop)

            if not new_suggested <= new_avail:
                # We're somehow asking for something not in the avail range we're asking for.
                self.logger.error("%s Calculated suggested %s not in available range %s", hop, new_suggested, new_avail)
                raise StitchingError("%s could not be processed: calculated a suggested VLAN of %s that is not in the calculated available range %s" % (hop, new_suggested, new_avail))

            # If we have a previous manifest, we might be done or might need to delete a previous reservation
            if hop._hop_link.vlan_suggested_manifest:
                if not hadPreviousManifest:
                    raise StitchingError("%s had no previous manifest, but its hop %s did" % (self, hop))
                if hop._hop_link.vlan_suggested_request != new_suggested:
                    # If we already have a result but used different input, then this result is suspect. Redo.
                    hop._hop_link.vlan_suggested_request = new_suggested
                    # if however the previous suggested_manifest == new_suggested, then maybe this is OK?
                    if hop._hop_link.vlan_suggested_manifest == new_suggested:
                        self.logger.info("%s VLAN suggested request %s != new request %s, but had manifest that is the new request, so leave it alone", hop, hop._hop_link.vlan_suggested_request, new_suggested)
                    else:
                        self.logger.info("Redo %s: had previous different suggested VLAN for hop %s (old request/manifest %s != new request %s)", self, hop, hop._hop_link.vlan_suggested_request, new_suggested)
                        mustDelete = True
                        alreadyDone = False
                else:
                    self.logger.debug("%s had previous manifest and used same suggested VLAN for hop %s (%s) - no need to redo", self, hop, hop._hop_link.vlan_suggested_request)
                    # So for this hop at least, we don't need to redo this AM
            else:
                alreadyDone = False
                # No previous result
                if hadPreviousManifest:
                    raise StitchingError("%s had a previous manifest but hop %s did not" % (self, hop))
                if hop._hop_link.vlan_suggested_request != new_suggested:
                    # FIXME: Comment out this log statement?
                    self.logger.debug("%s changing VLAN suggested from %s to %s", hop, hop._hop_link.vlan_suggested_request, new_suggested)
                    hop._hop_link.vlan_suggested_request = new_suggested
                else:
                    # FIXME: Comment out this log statement?
                    self.logger.debug("%s already had VLAN suggested %s", hop, hop._hop_link.vlan_suggested_request)

            # Now check the avail range as we did for suggested
            if hop._hop_link.vlan_range_manifest:
                if not hadPreviousManifest:
                    self.logger.error("%s had no previous manifest, but its hop %s did", self, hop)
                if hop._hop_link.vlan_range_request != new_avail:
                    # If we already have a result but used different input, then this result is suspect. Redo?
                    self.logger.debug("%s had previous manifest and used different avail VLAN range for hop %s (old request %s != new request %s)", self, hop, hop._hop_link.vlan_range_request, new_avail)
                    if hop._hop_link.vlan_suggested_manifest and hop._hop_link.vlan_suggested_manifest not in new_avail:
                        # new avail doesn't contain the previous manifest suggested. So new avail would have precluded
                        # using the suggested we picked before. So we have to redo
                        mustDelete = True
                        alreadyDone = False
                        self.logger.warn("%s previous availRange %s not same as new, and previous manifest suggested %s not in new avail %s - redo this AM", hop, hop._hop_link.vlan_range_request, hop._hop_link.vlan_suggested_manifest, new_avail)
                    else:
                        # what we picked before still works, so leave it alone
                        self.logger.info("%s had manifest suggested %s that works with new/different availRange %s - don't redo", hop, hop._hop_link.vlan_suggested_manifest, new_avail)
                        #self.logger.debug("%s had avail range manifest %s, and previous avail range request (%s) != new (%s), but previous suggested manifest %s is in the new avail range, so it is still good - no redo", hop, hop._hop_link.vlan_range_manifest, hop._hop_link.vlan_range_request, new_avail, hop._hop_link.vlan_suggested_manifest)

                    # Either way, record what we want the new request to be, so later if we redo we use the right thing
                    hop._hop_link.vlan_range_request = new_avail
                else:
                    # FIXME: move to debug?
                    self.logger.info("%s had previous manifest range and used same avail VLAN range request %s - no redo", hop, hop._hop_link.vlan_range_request)
            else:
                alreadydone = False
                # No previous result
                if hadPreviousManifest:
                    raise StitchingError("%s had a previous manifest but hop %s did not" % (self, hop))
                if hop._hop_link.vlan_range_request != new_avail:
                    self.logger.debug("%s changing avail VLAN from %s to %s", hop, hop._hop_link.vlan_range_request, new_avail)
                    hop._hop_link.vlan_range_request = new_avail
                else:
                    self.logger.debug("%s already had avail VLAN %s", hop, hop._hop_link.vlan_range_request)
        # End of loop over hops to copy VLAN tags over and see if this is a redo or we need to delete
        return mustDelete, alreadyDone

    def getEditedRSpecDom(self, originalRSpec):
        # For each path on this AM, get that Path to write whatever it thinks necessary into a
        # deep clone of the incoming RSpec Dom
        requestRSpecDom = originalRSpec.cloneNode(True)
        stitchNodes = requestRSpecDom.getElementsByTagName(RSpecParser.STITCHING_TAG)
        if stitchNodes and len(stitchNodes) > 0:
            stitchNode = stitchNodes[0]
        else:
            raise StitchingError("Couldn't find stitching element in rspec for %s request", self)

        domPaths = stitchNode.getElementsByTagName(RSpecParser.PATH_TAG)
#        domPaths = stitchNode.getElementsByTagNameNS(rspec_schema.STITCH_SCHEMA_V1, RSpecParser.PATH_TAG)
        for path in self.paths:
            #self.logger.debug("Looking for node for path %s", path)
            domNode = None
            if domPaths:
                for pathNode in domPaths:
                    pathNodeId = pathNode.getAttribute(Path.ID_TAG)
                    if pathNodeId == path.id:
                        domNode = pathNode
                        #self.logger.debug("Found node for path %s", path.id)
                        break
            if domNode is None:
                raise StitchingError("Couldn't find Path %s in stitching element of RSpec for %s request" % (path, self))
            #self.logger.debug("Doing path.editChanges for path %s", path.id)
            path.editChangesIntoDom(domNode)
        return requestRSpecDom

    # For a given hop, extract from the Manifest DOM a tuple (vlanRangeAvailability, suggestedVLANRange)
    def getVLANRangeSuggested(self, manifest, hop_id, path_id):
        vlan_range_availability = None
        suggested_vlan_range = None

        rspec_node = None
        stitching_node = None
        path_node = None
        this_path_id = ""
        hop_node = None
        link_node = None
        link_id = ""
        this_hop_id = ""
        scd_node = None
        scsi_node = None
        scsil2_node = None

        # FIXME: Call once for all hops

        for child in manifest.childNodes:
            if child.nodeType == XMLNode.ELEMENT_NODE and \
                    child.localName == RSpecParser.RSPEC_TAG:
                rspec_node = child
                break

        if rspec_node:
            for child in rspec_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == RSpecParser.STITCHING_TAG:
                    stitching_node = child
                    break
        else:
            raise StitchingError("%s: No rspec element in manifest" % self)

        if stitching_node:
            for child in stitching_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == RSpecParser.PATH_TAG:
                    this_path_id = child.getAttribute(Path.ID_TAG)
                    if this_path_id == path_id:
                        path_node = child
                        break
        else:
            raise StitchingError("%s: No stitching element in manifest" % self)

        if path_node:
#            self.logger.debug("%s Found rspec manifest stitching path %s (id %s)" % (self, path_node, this_path_id))
            for child in path_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == Path.HOP_TAG:
                    this_hop_id = child.getAttribute(Hop.ID_TAG)
                    if this_hop_id == hop_id:
                        hop_node = child
                        break
        else:
            raise StitchingError("%s: No stitching path %s element in manifest" % (self, path_id))

        if hop_node:
#            self.logger.debug("Found hop %s (id %s)" % (hop_node, this_hop_id))
            for child in hop_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == Hop.LINK_TAG:
                    link_node = child
                    break
        else:
            raise StitchingError("%s: Couldn't find hop %s in manifest rspec. Looking in path %s (id %s)" % (self, hop_id, path_node, this_path_id))

        if link_node:
            link_id = link_node.getAttribute(HopLink.ID_TAG)
#            self.logger.debug("Hop had link %s", link_id)
            for child in link_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == HopLink.SCD_TAG:
                    scd_node = child
                    break
        else:
            raise StitchingError("%s: Couldn't find link in hop %s in manifest rspec" % (self, hop_id))

        if scd_node:
            for child in scd_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == HopLink.SCSI_TAG:
                    scsi_node = child
                    break
        else:
            raise StitchingError("%s: Couldn't find switchingCapabilityDescriptor in hop %s in link %s in manifest rspec" % (self, hop_id, link_id))

        if scsi_node:
            for child in scsi_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE and \
                        child.localName == HopLink.SCSI_L2_TAG:
                    scsil2_node = child
                    break
        else:
            raise StitchingError("%s: Couldn't find switchingCapabilitySpecificInfo in hop %s in manifest rspec" % (self, hop_id))

        if scsil2_node:
            for child in scsil2_node.childNodes:
                if child.nodeType == XMLNode.ELEMENT_NODE:
                    child_text = child.childNodes[0].nodeValue
                    if child.localName == HopLink.VLAN_RANGE_TAG:
                        vlan_range_availability = child_text
                    elif child.localName == HopLink.VLAN_SUGGESTED_TAG:
                        suggested_vlan_range = child_text
        else:
            raise StitchingError("%s: Couldn't find switchingCapabilitySpecificInfo_L2sc in hop %s in manifest rspec" % (self, hop_id))

        return (vlan_range_availability, suggested_vlan_range)

    def doReservation(self, opts, slicename, scsCallCount):
        '''Reserve at this AM. Construct omni args, save RSpec to a file, call Omni,
        handle raised Exceptions, DCN AMs wait for status ready, and return the manifest
        '''

        # Ensure we have the right URL / API version / command combo
        # If this AM does APIv3, I'd like to use it
        # But the caller needs to know if we used APIv3 so they know whether to call provision later
        opName = 'createsliver'
        if self.api_version > 2:
            opName = 'allocate'

        self.allocateTries = self.allocateTries + 1

        # Write the request rspec to a string that we save to a file
        requestString = self.requestDom.toprettyxml()
        header = "<!-- Resource request for stitching for:\n\tSlice: %s\n\t at AM:\n\tURN: %s\n\tURL: %s\n -->" % (slicename, self.urn, self.url)
        if requestString and rspec_util.is_rspec_string( requestString, None, None, logger=self.logger ):
            # This line seems to insert extra \ns - GCF ticket #202
#            content = rspec_util.getPrettyRSpec(requestString)
            content = stripBlankLines(string.replace(requestString, "\\n", '\n'))
        else:
            raise StitchingError("%s: Constructed request RSpec malformed? Begins: %s" % (self, requestString[:100]))
        self.rspecfileName = _construct_output_filename(opts, slicename, self.url, self.urn, \
                                                       opName + '-request-'+str(scsCallCount) + str(self.allocateTries), '.xml', 1)

        # Put request RSpecs in /tmp - ensure writable
        # FIXME: Commandline users would prefer something else?
        self.rspecfileName = Aggregate.REQ_RSPEC_DIR + "/" + self.rspecfileName

        # Set -o to ensure this request RSpec goes to a file, not logger or stdout
        opts_copy = copy.deepcopy(opts)
        opts_copy.output = True

        _printResults(opts_copy, self.logger, header, content, self.rspecfileName)
        self.logger.info("Saved AM %s new request RSpec to file %s", self.urn, self.rspecfileName)

        # Set opts.raiseErrorOnV2AMAPIError so we can see the error codes and respond directly
        # In WARN mode, do not write results to a file. And note results also won't be in log (they are at INFO level)
        if opts.warn:
            # FIXME: Clear opts.debug, .info, .tostdout?
            omniargs = ['--raise-error-on-v2-amapi-error', '-V%d' % self.api_version, '-a', self.url, opName, slicename, self.rspecfileName]
        else:
            omniargs = ['-o', '--raise-error-on-v2-amapi-error', '-V%d' % self.api_version, '-a', self.url, opName, slicename, self.rspecfileName]
            
        self.logger.info("\n\tStitcher doing %s at %s", opName, self.url)
        self.logger.debug("omniargs %r", omniargs)

        result = None

        try:
            # FIXME: Is that the right counter there?
            (text, result) = self.doAMAPICall(omniargs, opts, opName, slicename, self.allocateTries)
            self.logger.debug("%s %s at %s got: %s", opName, slicename, self, text)
            if "PG log url" in text:
                pgInd = text.find("PG log url - look here for details on any failures: ")
                self.pgLogUrl = text[pgInd + len("PG log url - look here for details on any failures: "):text.find(")", pgInd)]
                self.logger.debug("Had PG log url in return text and recorded: %s", self.pgLogUrl)
            elif result and isinstance(result, dict) and len(result.keys()) == 1 and \
                    result.itervalues().next().has_key('code') and \
                    isinstance(result.itervalues().next()['code'], dict):
                code = result.itervalues().next()['code']
                try:
                    self.pgLogUrl = code["protogeni_error_url"]
                    self.logger.debug("Got PG Log url from return struct %s", self.pgLogUrl)
                except:
                    pass
            elif self.api_version >= 3 or result is None:
                # malformed result
                msg = "%s got Malformed return from %s: %s" % (self, opName, text)
                self.logger.error(msg)
                # FIXME: Retry before going to the SCS? Or bail altogether?
                self.inProcess = False
                raise StitchingError(msg)

            # May have changed URL versions - if so, save off the corrected URL?
            if result and self.api_version > 2:
                url = result.iterkeys().next()
                if str(url) != str(self.url):
                    self.logger.debug("%s found URL for API version is %s", self, url)
                    # FIXME: Safe to change the local URL to the corrected one?
#                    self.url = url

            if self.api_version == 2 and result:
                # Success in APIv2
                pass
            elif self.api_version >= 3 and result and isinstance(result, dict) and len(result.keys()) == 1 and \
                    result.itervalues().next().has_key("code") and \
                    isinstance(result.itervalues().next()["code"], dict) and \
                    result.itervalues().next()["code"].has_key("geni_code"):
                if result.itervalues().next()["code"]["geni_code"] != 0:
                    #self.logger.debug("APIv3 result struct OK but non 0")
                    # The struct in the AMAPIError is just the return value and not by URL
                    raise AMAPIError(text, result.itervalues().next())
                # Else this is success
                #self.logger.debug("APIv3 proper result struct - success")
            else:
                if self.api_version == 2:
                    msg = "%s got Empty v2 return from %s: %s" % (self, opName, text)
                else:
                    msg = "%s got Malformed v3+ return from %s: %s" % (self, opName, text)
                self.logger.error(msg)
                # FIXME: Retry before going to the SCS? Or bail altogether?
                self.inProcess = False
                raise StitchingError(msg)

        except AMAPIError, ae:
            self.logger.warn("Got AMAPIError doing %s %s at %s: %s", opName, slicename, self, ae)
            if ae.returnstruct and isinstance(ae.returnstruct, dict) and ae.returnstruct.has_key("code") and \
                    isinstance(ae.returnstruct["code"], dict) and ae.returnstruct["code"].has_key("geni_code"):

                # Try to get PG log url:
                try:
                    if ae.returnstruct["code"]["am_type"] == "protogeni":
                        self.pgLogUrl = ae.returnstruct["code"]["protogeni_error_url"]
                except:
                    pass

                if ae.returnstruct["code"]["geni_code"] == 24:
                    # VLAN_UNAVAILABLE
                    self.logger.warn("FIXME: Got VLAN_UNAVAILABLE from %s %s at %s", opName, slicename, self)
                    # FIXME FIXME FIXME
                    self.handleVlanUnavailable(opName, ae)
                else:
                    # some other AMAPI error code
                    # FIXME: Try to parse the am_code or the output message to decide if this is 
                    # a stitching error (go to SCS) vs will never work (go to user)?
                    # This is where we have to distinguish node unavailable vs VLAN unavailable vs something else

                    isVlanAvailableIssue = False
                    # PG based AMs seem to return a particular error code and string
                    try:
                        code = ae.returnstruct["code"]["geni_code"]
                        amcode = ae.returnstruct["code"]["am_code"]
                        amtype = ae.returnstruct["code"]["am_type"]
                        msg = ae.returnstruct["output"]
#                        self.logger.debug("Error was code %s (am code %s): %s", code, amcode, msg)
                        if ("Could not reserve vlan tags" in msg and code==2 and amcode==2 and amtype=="protogeni") or \
                                ('vlan tag for ' in msg and ' not available' in msg and code==1 and amcode==1 and amtype=="protogeni"):
#                            self.logger.debug("Looks like a vlan availability issue")
                            isVlanAvailableIssue = True
                    except:
#                        self.logger.debug("Apparently not a vlan availability issue. Back to the SCS")
                        pass

                    if isVlanAvailableIssue:
                        self.handleVlanUnavailable(opName, ae)
                    else:
                        # Exit to SCS
                        if not self.userRequested:
                            # If we've tried this AM a few times, set its hops to be excluded
                            if self.allocateTries > self.MAX_TRIES:
                                self.logger.debug("%s allocation failed %d times - try excluding its hops", self, self.allocateTries)
                                for hop in self.hops:
                                    hop.excludeFromSCS = True
                        # This says always go back to the SCS
                        # This is dangerous - we could thrash
                        # FIXME: go back a limited # of times
                        # FIXME: Uncomment below code so only errors at SCS AMs cause us to go back to SCS?
                        self.inProcess = False
                        raise StitchingCircuitFailedError("Circuit reservation failed at %s (%s). Try again from the SCS" % (self, ae))

#                        if not self.userRequested:
#                            # Exit to SCS
#                            # If we've tried this AM a few times, set its hops to be excluded
#                            if self.allocateTries > self.MAX_TRIES:
#                                self.logger.debug("%s allocation failed %d times - try excluding its hops", self, self.allocateTries)
#                                for hop in self.hops:
#                                    hop.excludeFromSCS = True
#                            self.inProcess = False
#                            raise StitchingCircuitFailedError("Circuit failed at %s (%s). Try again from the SCS" % (self, ae))
#                        else:
#                            # Exit to User
#                            raise StitchingError("Stitching failed trying %s at %s: %s" % (opName, self, ae))
            else:
                # Malformed AMAPI return struct
                # Exit to User
                raise StitchingError("Stitching failed: Malformed error struct doing %s at %s: %s" % (opName, self, ae))
        except Exception, e:
            # Some other error (OmniError, StitchingError)
            # Exit to user
            raise StitchingError(e) # FIXME: right way to re-raise?

        # Pull actual manifest out of result
        # If v2, this already is the manifest
        if self.api_version > 2:
            try:
                result = result.itervalues().next()['value']['geni_rspec']
            except Exception, e:
                # FIXME: Do this even if not fakeModeDir?
                if (isinstance(result, str) or isinstance(result, unicode)) and opts.fakeModeDir:
                    # Well OK then
                    pass
                else:
                    msg = "Malformed return struct from %s at %s: %s (result: %s)" % (opName, self, e, result)
                    self.logger.warn(msg)
                    raise StitchingError("Stitching failed - got %s" % msg)

        # Handle DCN AMs
        if self.dcn:
            # FIXME: right counter?
            (text, result) = self.handleDcnAM(opts, slicename, self.allocateTries)

        # Caller handles saving the manifest, comparing man sug with request, etc
        # FIXME: Not returning text here. Correct?
        return result

    def handleDcnAM(self, opts, slicename, ctr):
        # DCN based AMs cannot really tell you if they succeeded until sliverstatus is ready or not
        # So wait for that, then get the listresources manifest and use that as the manifest

        self.logger.info("DCN AM %s: must wait for status ready....", self)

        # FIXME: Add a maxtime to wait as well
        tries = 0
        status = 'unknown'
        while tries < self.SLIVERSTATUS_MAX_TRIES:
            # generate args for sliverstatus
            if self.api_version == 2:
                opName = 'sliverstatus'
            else:
                opName = 'status'
            if opts.warn:
                omniargs = [ '-V%d' % self.api_version, '--raise-error-on-v2-amapi-error', '-a', self.url, opName, slicename]
            else:
                omniargs = ['-o', '-V%d' % self.api_version, '--raise-error-on-v2-amapi-error', '-a', self.url, opName, slicename]
            result = None
            try:
                tries = tries + 1
                # FIXME: shouldn't ctr be based on tries here?
                # FIXME: Big hack!!!
                if not opts.fakeModeDir:
                    (text, result) = self.doAMAPICall(omniargs, opts, opName, slicename, ctr)
                    self.logger.debug("handleDcn %s %s at %s got: %s", opName, slicename, self, text)
            except Exception, e:
                # exit gracefully
                # FIXME: to SCS excluding this hop? to user? This could be some transient thing, such that redoing
                # circuit as is would work. Or it could be something permanent. How do we know?
                raise StitchingError("%s %s failed at %s: %s" % (opName, slicename, self, e))

            # Parse out sliver status / status
            if isinstance(result, dict) and result.has_key(self.url) and result[self.url] and \
                    isinstance(result[self.url], dict):
                if self.api_version == 2:
                    if result[self.url].has_key("geni_status"):
                        status = result[self.url]["geni_status"]
                    else:
                        # else malformed
                        raise StitchingError("%s had malformed %s result in handleDCN" % (self, opName))
                else:
                    if result[self.url].has_key("value") and isinstance(result[self.url]["value"], dict) and \
                            result[self.url]["value"].has_key("geni_slivers") and isinstance(result[self.url]["value"]["geni_slivers"], list):
                        for sliver in result[self.url]["value"]["geni_slivers"]:
                            if isinstance(sliver, dict) and sliver.has_key("geni_allocation_status"):
                                status = sliver["geni_allocation_status"]
                                break
                        # FIXME: Which sliver(s) do I look at?
                        # 1st? look for any not ready and take that?
                        # And pull out any geni_error
                        # FIXME FIXME
                        # FIXME: I don't really know how AMs will do v3 status' so wait
                    else:
                        # malformed
                        raise StitchingError("%s had malformed %s result in handleDCN" % (self, opName))
            else:
                # FIXME FIXME Big hack
                if not opts.fakeModeDir:
                    # malformed
                    raise StitchingError("%s had malformed %s result in handleDCN" % (self, opName))

            # FIXME: Big hack!!!
            if opts.fakeModeDir:
                status = 'ready'

            status = str(status).lower().strip()
            if status in ('failed', 'ready', 'geni_allocated', 'geni_provisioned', 'geni_failed', 'geni_notready', 'geni_ready'):
                break
        # End of while loop getting sliverstatus

        if status not in ('ready', 'geni_allocated', 'geni_provisioned', 'geni_ready'):
            self.logger.warn("%s is %s at %s. Treat as VLAN unavailable.", opName, status, self)
            # deleteReservation
            opName = 'deletesliver'
            if self.api_version > 2:
                opName = 'delete'
            if opts.warn:
                omniargs = ['--raise-error-on-v2-amapi-error', '-V%d' % self.api_version, '-a', self.url, opName, slicename]
            else:
                omniargs = ['--raise-error-on-v2-amapi-error', '-o', '-V%d' % self.api_version, '-a', self.url, opName, slicename]
            try:
                # FIXME: right counter?
                (text, delResult) = self.doAMAPICall(omniargs, opts, opName, slicename, ctr)
                self.logger.debug("handleDCN %s %s at %s got: %s", opName, slicename, self, text)
            except Exception, e:
                # Exit to user
                raise StitchingError("Failed to delete reservation at DCN AM %s that was %s: %s" % (self, status, e))

            # Treat as VLAN was Unavailable - note it could have been a transient circuit failure or something else too
            self.handleVlanUnavailable(opName, "Sliver status was %s" % status)
        else:
            # Status is ready
            # generate args for listresources
            if self.api_version == 2:
                opName = 'listresources'
            else:
                opName = 'describe'
            # FIXME: Big hack!!!
            if opts.fakeModeDir:
                if self.api_version == 2:
                    opName = 'createsliver'
                else:
                    opName = 'allocate'
                self.logger.info("Will look like %s, but pretending to do listresources", opName)
            if opts.warn:
                omniargs = ['--raise-error-on-v2-amapi-error', '-V%d' % self.api_version, '-a', self.url, opName, slicename]
            else:
                omniargs = ['-o', '--raise-error-on-v2-amapi-error', '-V%d' % self.api_version, '-a', self.url, opName, slicename]
            try:
                (text, result) = self.doAMAPICall(omniargs, opts, opName, slicename, ctr)
                self.logger.debug("%s %s at %s got: %s", opName, slicename, self, text)
            except Exception, e:
                # Note this could be an AMAPIError. But what AMAPIError could this be that we could handle?
                # Exit gracefully
                raise StitchingError("Stitching failed in handleDcn trying %s at %s: %s" % (opName, self, e))

            # Get the single manifest out of the result struct
            try:
                if self.api_version == 2:
                    oneResult = result.values()[0]["value"]
                elif self.api_version == 1:
                    oneResult = result.values()[0]
                else:
                    oneResult = result.values()[0]["value"]["geni_rspec"]
            except Exception, e:
                if (isinstance(result, str) or isinstance(result, unicode)) and opts.fakeModeDir:
                    oneResult = result
                else:
                    raise StitchingError("Malformed return from %s at %s: %s" % (opName, self, e))
            return (text, oneResult)

    def handleSuggestedVLANNotRequest(self, opts, slicename):
        # FIXME FIXME FIXME

        # note what we tried that failed (ie what was requested but not given at this hop)
        for hop in self.hops:
            if hop._hop_link.vlan_suggested_manifest and len(hop._hop_link.vlan_suggested_manifest) > 0 and \
                    hop._hop_link.vlan_suggested_request != hop._hop_link.vlan_suggested_manifest:
                hop.vlans_unavailable = hop.vlans_unavailable.union(hop._hop_link.vlan_suggested_request)

#      find an AM to redo
#        note VLAN tags in manifest that don't work later as vlan_unavailable on that AM
#           FIXME: Or separate that into vlan_unavailable and vlan_tried?
#        FIXME: AMs need a redo or reservation counter maybe, to avoid thrashing?
#        FIXME: mark that we are redoing?
#            idea: do we need this in allocate() on the AM we are redoing?
#        delete that AM
#            thatAM.deleteReservation(opts, slicename)
#        set request VLAN tags on that AM
            # See logic in copyVLANsAndDetectRedo
#            thatAM.somehop.hop_link.vlan_suggested_request = self.someOtherHop.hop_link.vlan_suggested_manifest
#         In avail, be sure not to include VLANs we already had once that clearly failed (see vlans_unavailable)
#        exit from this AM without setting complete, so we recheck VLAN tag consistency later
#      else (no AM to redo) gracefully exit: raiseStitchingCircuitFailedError

# From wiki
# This is where suggested was single and man != request. Find the AM that picked the regjected tag if any, and redo that AM: delete
# the existing reservation, set its requested = this manifest, excluding from its avail the requested suggested here that we didn't pick

# Also add to hop.vlan_unavailable the suggested we didnt pick

# If not hop.import_vlans: no prior AMs need to agree, so return as though this succeeded
# Set flag for in process stuff to pause
# go to the hop.import_Vlans_from hop. Call that hop2
# if hop2 requested == manifest, recurse to its import_from
# (possible extraneous else: hop2 is same AM as hop1 and hop2 request = hop1 request and hop2 man == hop1 man, then recurse)
# else if hop2 same AM as hop1 - huh? raise StitchingError to go to user
# else if requested == 'any', then this AM picked the offending tag
#  thatAM.deleteReservation
#  set hop2.suggested_request = hop1.suggested_manifest
#  set hop2.range_request -= hop1.suggested_request
#  return
# else: huh? raise StitchingError
# else can't find an AM to start over from: raise StitchingCircuitFailedError
        pass

    def handleVlanUnavailable(self, opName, exception, failedHop=None, suggestedWasNull=False):
#        FIXME: See logic on wiki
#        remember unavailable vlanRangeAvailability on the hop
#        may need to mark hop explicity loose or add to hop_exclusion list for next SCS request
#        may need to go back to SCS or go back to user
#        if negotiate:
#           don't mark this AM complete, so dependencies don't start going
#           find AM to redo from
#            note VLAN tags in manifest that don't work later as vlan_unavailable
#              FIXME: Or separate that into vlan_unavailable and vlan_tried?
#            FIXME: AMs need a redo or reservation counter maybe, to avoid thrashing?
#            FIXME: mark that we are redoing?
#                idea: do we need this in allocate() on the AM we are redoing?
#            delete reservation at that AM, and exit out from this AM in a graceful way (not setting .complete)

# Wiki logic
# Remember which tag was unavailable in hop.vlans_unavailable if I can tell. Plan to exclude that from SCS vlanRangeAvailability,
# if SCS supports that.

        if not failedHop and len(self.hops) == 1:
            failedHop = iter(self.hops).next()
#            self.logger.debug("handleVlanUnavail got no specific failed hop, but AM only has hop %s", failedHop)

        if failedHop:
#            self.logger.debug("handleVU noting failedHop unavail vlan %s", failedHop._hop_link.vlan_suggested_request)
            failedHop.vlans_unavailable = failedHop.vlans_unavailable.union(failedHop._hop_link.vlan_suggested_request)

        # If the hops do not do vlan_translation, then they all have this issue
        for hop in self.hops:
            if not hop._hop_link.vlan_xlate:
#                self.logger.debug("handleVU noting doesnt-xlate unavail vlan %s", failedHop._hop_link.vlan_suggested_request)
                hop.vlans_unavailable = hop.vlans_unavailable.union(hop._hop_link.vlan_suggested_request)
            elif not failedHop:
                hop.vlans_unavailable = hop.vlans_unavailable.union(hop._hop_link.vlan_suggested_request)

# If this AM was a redo, this may be an irrecoverable failure. If vlanRangeAvailability was a range for the later AM, maybe.
  # Otherwise, raise StitchingCircuitFailedError to go back to the SCS and hope the SCS picks something else
# Set some kind of flag so in process stuff pauses (for threading)
# If APIv2 (and AM fails request if suggested unavail)
  # If suggested ANY then find AM where vlanRange was narrowed and redo there
  # Else suggested was single and vlanRange was a range --- FIXME

        canRedoRequestHere = True
        for hop in self.hops:
            if hop.import_vlans:
                # Some hops here depend on other AMs. This is a negotiation kind of case
#                self.logger.debug("%s imports vlans - so cannot redo here", hop)
                canRedoRequestHere = False
                break
            if len(hop._hop_link.vlan_range_request) <= 1:
                # Only the 1 VLAN tag was in the available range
                self.logger.debug("%s has only 1 VLAN in the avail range, so cannot redo here", hop)
                canRedoRequestHere = False
                break
        if canRedoRequestHere and not (failedHop and suggestedWasNull) and isinstance(exception, AMAPIError) and exception.returnstruct:
#            self.logger.debug("%s failed request. Does not depend on others so maybe redo?", self)
            # Does the error look like the particular tag just wasn't currently available?
            try:
                code = exception.returnstruct["code"]["geni_code"]
                amcode = exception.returnstruct["code"]["am_code"]
                amtype = exception.returnstruct["code"]["am_type"]
                msg = exception.returnstruct["output"]
                self.logger.debug("Error was code %d (am code %d): %s", code, amcode, msg)
#                # FIXME: If we got an empty / None / null suggested value on the failedHop
                # in a manifest, then we could also redo
                if code == 24 or ("Could not reserve vlan tags" in msg and code==2 and amcode==2 and amtype=="protogeni") or \
                        ('vlan tag for ' in msg and ' not available' in msg and code==1 and amcode==1 and amtype=="protogeni"):
#                    self.logger.debug("Looks like a vlan availability issue")
                    pass
                else:
                    self.logger.debug("handleVU says this isn't a vlan availability issue. Got error %d, %d, %s", code, amcode, msg)
                    canRedoRequestHere = False
            except Exception, e2:
                canRedoRequestHere = False
                self.logger.debug("handleVU Exception getting msg/code from exception %s: %s", exception, e2)
#        else:
            # FIXME: If canRedoRequestHere does this still hold?

# Next criteria: If there are hops that depend on this 
# that do NOT do vlan translation AND have other hops that in turn depend on those hops, 
# then there are too many variables - give up.

        if canRedoRequestHere:
            for depAgg in self.isDependencyFor:
                aggOK = True
                for hop in depAgg.hops:
                    if hop._hop_link.vlan_xlate:
                        continue
                    if not hop.import_vlans:
                        continue
                    # If this hop does not depend on the self AM, continue
                    thisAM = False
                    if hop.dependsOn:
                        for depHop in hop.dependsOn:
                            if depHop.aggregate == self:
                                thisAM = True
                                # depHop is the local hop that hop imports from / depends on
                                if failedHop and failedHop != depHop:
                                    # But it isn't the failed hop that is a problem. Does this mean this is OK?
                                    # FIXME FIXME
                                    pass
                                break
                    if not thisAM:
                        # Can this be? For a particular hop, maybe. But not for all
                        continue

                    # OK so we found a hop that depends on this Aggregate and does not do vlan translation
                    # Like PG-Utah depending on IG-Utah.
#                    self.logger.debug("%s does not do VLAN xlate and depends on this AM", depAgg)

                    # That's OK if that's it. But if that aggregate has other dependencies, then
                    # this is too complicated. It could still be OK, but it's too complicated
                    if hop.aggregate.isDependencyFor and len(hop.aggregate.isDependencyFor) > 0 \
                            and iter(hop.aggregate.isDependencyFor).next():
                        self.logger.debug("dependentAgg %s's hop %s's Aggregate is a dependency for others - cannot redo here", depAgg, hop)
                        canRedoRequestHere=False
                        aggOK = False
                        break
                # End of loop over hops in the dependent agg
                if not aggOK:
#                    self.logger.debug("depAgg %s has an issue - cannot redo here", depAgg)
                    canRedoRequestHere=False
                    break
            # end of loop over Aggs that depend on self

        if canRedoRequestHere:
#            self.logger.debug("After all checks looks like we can locally redo request for %s", self)
            hop = None
            newSug = ""
            oldSug = ""
            for hop in self.hops:
                # Edit vlan ranges only for the failed hop if set
                if failedHop and hop != failedHop:
                    continue

                # pull old suggested out of range
                hop._hop_link.vlan_range_request = hop._hop_link.vlan_range_request - hop._hop_link.vlan_suggested_request
#                # FIXME: Also pull that out of the manifest?
#                if hop._hop_link.vlan_range_manifest and len(hop._hop_link.vlan_range_manifest) > 0:
#                    hop._hop_link.vlan_range_manifest = hop._hop_link.vlan_range_manifest - hop._hop_link.vlan_suggested_request

                oldSug = hop._hop_link.vlan_suggested_request

                # If self is a VLAN producer, then set newSug to VLANRange('any') and let it pick?
                if hop._hop_link.vlan_producer:
                    newSug = VLANRange('any')
                else:
                    # Pick a random tag from range
                    newSug = iter(hop._hop_link.vlan_range_request).next()

                # Set that as suggested
                hop._hop_link.vlan_suggested_request = VLANRange(newSug)
                self.logger.debug("handleUn on %s doing local retry: set Avail=%s, Sug=%s (Sug was %s)", hop, hop._hop_link.vlan_range_request, newSug, oldSug)

                # FIXME: Set hops that depend on this hop to the proper values, or let those happen naturaly?

            self.inProcess = False
            if failedHop:
                msg = "Retry %s with %s new suggested %s (not %s)" % (self, hop, newSug, oldSug)
            else:
                msg = "Retry %s with new suggested VLANs" % (self)
            # This error is caught by Launcher, causing this AM to be put back in the ready pool
            raise StitchingRetryAggregateNewVlanError(msg)

        self.logger.debug("%s failure could not be redone locally", self)

        # If we got here, we can't handle this locally
        if not self.userRequested:
            # Exit to SCS
            # If we've tried this AM a few times, set its hops to be excluded
            if self.allocateTries > self.MAX_TRIES:
                self.logger.debug("%s allocation failed %d times - try excluding its hops", self, self.allocateTries)
                for hop in self.hops:
                    self.logger.debug
                    hop.excludeFromSCS = True
            self.inProcess = False
            raise StitchingCircuitFailedError("Circuit reservation failed at %s. Try again from the SCS" % self)
        else:
            # Exit to User
            raise StitchingError("Stitching failed trying %s at %s: %s" % (opName, self, exception))
# FIXME FIXME: Go back to SCS here too? Or will that thrash?
#            self.inProcess = False
#            raise StitchingCircuitFailedError("Circuit failed at %s. Try again from the SCS" % self)

    def deleteReservation(self, opts, slicename):
        '''Delete any previous reservation/manifest at this AM'''
        self.completed = False
        
        # Clear old manifests
        self.manifestDom = None
        for hop in self.hops:
            hop._hop_link.vlan_suggested_manifest = None
            hop._hop_link.vlan_range_manifest = None

        # Now mark all AMs that depend on this AM as incomplete, so we'll try them again
        # FIXME: This makes everything in chain get redone. Could we mark only the immediate
        # children, so only if those get deleted do their children get marked? Note the cost
        # isn't so high - it means falling into this code block and doing the above logic
        # that discovers existing manifests
        for agg in self.isDependencyFor:
            agg.completed = False

        # FIXME: Set a flag marking it is being deleted? Set inProcess?

        # Delete the previous reservation
        # FIXME: Do we do something with log level or log format or file for omni calls?
        # FIXME: Supply --raiseErrorOnAMAPIV2Error?
        opName = 'deletesliver'
        if self.api_version > 2:
            opName = 'delete'
        if opts.warn:
            omniargs = ['-V%d' % self.api_version,'--raise-error-on-v2-amapi-error', '-a', self.url, opName, slicename]
        else:
            omniargs = ['-V%d' % self.api_version,'--raise-error-on-v2-amapi-error', '-o', '-a', self.url, opName, slicename]

        self.logger.info("Doing %s at %s", opName, self.url)
        if not opts.fakeModeDir:
            try:
                self.inProcess = True
#                (text, (successList, fail)) = self.doOmniCall(omniargs, opts)
                (text, result) = self.doAMAPICall(omniargs, opts, opName, slicename, 1)
                self.inProcess = False
                if self.api_version == 2:
                    (successList, fail) = result
                    if not self.url in successList:
                        raise StitchingError("Failed to delete prior reservation at %s: %s" % (self.url, text))
                    else:
                        self.logger.debug("%s %s Result: %s", opName, self, text)
                else:
                    # API v3
                    retCode = 0
                    try:
                        retCode = result[self.url]["code"]["geni_code"]
                    except:
                        # Malformed return - treat as error
                        raise StitchingError("Failed to delete prior reservation at %s (malformed return): %s" % (self.url, text))
                    if retCode != 0:
                        raise StitchingError("Failed to delete prior reservation at %s: %s" % (self.url, text))
                    # need to check status of slivers to ensure they are all deleted
                    try:
                        for sliver in result[self.url]["value"]:
                            status = sliver["geni_allocation_status"]
                            if status != 'geni_unallocated':
                                if sliver.has_key("geni_error"):
                                    text = text + "; " + sliver["geni_error"]
                                raise StitchingError("Failed to delete prior reservation at %s for sliver %s: %s" % (self.url, sliver["geni_sliver_urn"], text))
                    except:
                        # Malformed return I think
                        raise StitchingError("Failed to delete prior reservation at %s (malformed return): %s" % (self.url, text))

            except OmniError, e:
                self.inProcess = False
                self.logger.error("Failed to %s at %s: %s", opName, self, e)
                raise StitchingError(e) # FIXME: Right way to re-raise?

        self.inProcess = False
        # FIXME: Fake mode delete results from a file?

        # FIXME: Set a flag marking this AM was deleted?
        return

    # This needs to handle createsliver, allocate, sliverstatus, listresources at least
    def doAMAPICall(self, args, opts, opName, slicename, ctr):
        # FIXME: Take scsCallCount as well?
        gotBusy = False
        busyCtr = 0
        while busyCtr < self.BUSY_MAX_TRIES:
            try:
                ctr = ctr + 1
                if opts.fakeModeDir:
                    (text, result) = self.fakeAMAPICall(args, opts, opName, slicename, ctr)
                else:
                    (text, result) = self.doOmniCall(args, opts)
                break # Not an error - breakout of loop
            except AMAPIError, ae:
                if is_busy_reply(ae.returnstruct):
                    self.logger.debug("%s got BUSY doing %s", self, opName)
                    time.sleep(self.BUSY_POLL_INTERVAL_SEC)
                    busyCtr = busyCtr + 1
                else:
                    raise ae
        return (text, result)

    def doOmniCall(self, args, opts):
        # spawn a thread if threading
        return omni.call(args, opts)

    # This needs to handle createsliver, allocate, sliverstatus, listresources at least
    # FIXME FIXME: Need more fake result files and to clean this all up! ****
    def fakeAMAPICall(self, args, opts, opName, slicename, ctr):
        # FIXME: Take scsCallCount as well?
        self.logger.info("Doing FAKE %s at %s", opName, self)

        # FIXME: Maybe take the request filename and make a -p arg for finding the canned files?
        # Or if I really use the scs, save the SCS in a file with the -p so I can find it here?

        # derive filename
        # FIXME: Take the expanded request from the SCS and pretend it is the manifest
        # That way, we get the VLAN we asked for
        resultPath = Aggregate.FAKEMODESCSFILENAME

#        # For now, results file only has a manifest. No JSON
#        resultFileName = _construct_output_filename(opts, slicename, self.url, self.urn, opName+'-result'+str(ctr), '.json', 1)
#        resultPath = os.path.join(opts.fakeModeDir, resultFileName)
#        if not os.path.exists(resultPath):
#            resultFileName = _construct_output_filename(opts, slicename, self.url, self.urn, opName+'-result'+str(ctr), '.xml', 1)
#            resultPath = os.path.join(opts.fakeModeDir, resultFileName)
        if not resultPath or not os.path.exists(resultPath):
            if opName in ("allocate", "createsliver"):
                # Fallback fake mode behavior
                time.sleep(random.randrange(1, 6))
                for hop in self.hops:
                    hop._hop_link.vlan_suggested_manifest = hop._hop_link.vlan_suggested_request
                    hop._hop_link.vlan_range_manifest = hop._hop_link.vlan_range_request
                self.logger.warn("Did fallback fake mode allocate")
                msg = "Did fallback fake %s" % opName
                return (msg, msg)
            else:
                raise StitchingError("Failed to find fake results file using %s" % resultFileName)

        self.logger.info("Reading FAKE %s results from %s", opName, resultPath)
        resultJSON = None
        result = None
        # Read in the file, trying as JSON
        try:
            with open(resultPath, 'r') as file:
                resultsString = file.read()
            try:
                resultJSON = json.loads(resultsString, encoding='ascii')
            except Exception, e2:
#                self.logger.debug("Failed to read fake results as json: %s", e2)
                result = resultsString
        except Exception, e:
            self.logger.error("Failed to read result string from %s: %s", resultPath, e)
            # FIXME
            raise e
        if resultJSON:
            # Got JSON - check for normal return struct
            if isinstance(resultJSON, dict) and resultJSON.has_key("code") and isinstance(resultJSON["code"], dict) and \
                    resultJSON["code"].has_key("geni_code"):
                # If success, return the value as the result
                if resultJSON["code"]["geni_code"] == 0:
                    if resultJSON["code"].has_key("value"):
                        result = resultJSON["code"]["value"]
                    else:
                        raise StitchingError("Malformed result struct from %s claimed success but had no value" % resultPath)
                else:
                    # Not success - raise it as an AMAPIError
                    raise AMAPIError("Fake failure doing %s at %s from file %s" % (opName, self.url, resultPath), resultJSON)
            else:
                # Malformed struct - maybe this is a real result as is
                result = resultJSON
        else:
            # Not JSON - return as real result
            result = resultsString
        return ("Fake %s at %s from file %s" % (opName, self.url, resultPath), result)

class Hop(object):
    # A hop on a path in the stitching element
    # Note this is path specific (and has a path reference)

    # XML tag constants
    ID_TAG = 'id'
    TYPE_TAG = 'type'
    LINK_TAG = 'link'
    NEXT_HOP_TAG = 'nextHop'

    @classmethod
    def fromDOM(cls, element):
        """Parse a stitching hop from a DOM element."""
        # FIXME: getAttributeNS?
        id = element.getAttribute(cls.ID_TAG)
        isLoose = False
        if element.hasAttribute(cls.TYPE_TAG):
            hopType = element.getAttribute(cls.TYPE_TAG)
            if hopType.lower().strip() == 'loose':
                isLoose = True
        hop_link = None
        next_hop = None
        for child in element.childNodes:
            if child.localName == cls.LINK_TAG:
                hop_link = HopLink.fromDOM(child)
            elif child.localName == cls.NEXT_HOP_TAG:
                next_hop = child.firstChild.nodeValue
                if next_hop == 'null':
                    next_hop = None
        hop = Hop(id, hop_link, next_hop)
        if isLoose:
            hop.loose = True
        return hop

    def __init__(self, id, hop_link, next_hop):
        self._id = id
        self._hop_link = hop_link
        self._next_hop = next_hop
        self._path = None
        self._aggregate = None
        self._import_vlans = False
        self._dependencies = []
        self.idx = None
        self.logger = logging.getLogger('stitch.Hop')
        self.import_vlans_from = None # a pointer to another hop

        # If True, then next request to SCS should explicitly
        # mark this hop as loose
        self.loose = False

        # Set to true so later call to SCS will explicitly exclude this Hop
        self.excludeFromSCS = False

        # VLANs we know are not possible here - cause of VLAN_UNAVAILABLE
        # or cause a suggested was not picked.
        # Use this to avoid picking these later
        self.vlans_unavailable = VLANRange()

    def __str__(self):
        return "<Hop %r on path %r>" % (self.urn, self._path.id)

    @property
    def urn(self):
        return self._hop_link and self._hop_link.urn

    @property
    def aggregate(self):
        return self._aggregate

    @property
    def path(self):
        return self._path

    @path.setter
    def path(self, path):
        self._path = path

    @aggregate.setter
    def aggregate(self, agg):
        self._aggregate = agg

    @property
    def import_vlans(self):
        return self._import_vlans

    @import_vlans.setter
    def import_vlans(self, value):
        self._import_vlans = value

    @property
    def dependsOn(self):
        return self._dependencies

    def add_dependency(self, hop):
        self._dependencies.append(hop)

    def editChangesIntoDom(self, domHopNode):
        '''Edit any changes made in this element into the given DomNode'''
        # Note the parent RSpec object's dom is not touched, unless the given node is from that document
        # Here we just like the HopLink do its thing

        # Incoming node should be the node for this hop
        nodeId = domHopNode.getAttribute(self.ID_TAG)
        if nodeId != self._id:
            raise StitchingError("%s given Dom node with different Id: %s" % (self, nodeId))

        # Mark hop explicitly loose if necessary
        if self.loose:
            domHopNode.setAttribute(self.TYPE_TAG, 'loose')

        for child in domHopNode.childNodes:
            if child.localName == self.LINK_TAG:
#                self.logger.debug("%s editChanges calling _hop_link with node %r", self, child)
                self._hop_link.editChangesIntoDom(child)

class RSpec(GENIObject):
    '''RSpec'''
    __simpleProps__ = [ ['stitching', Stitching] ]

    def __init__(self, stitching=None): 
        super(RSpec, self).__init__()
        self.stitching = stitching
        self._nodes = []
        self._links = [] # Main body links
        # DOM used to construct this: edits to objects are not reflected here
        self.dom = None
        # Note these are not Aggregate objects to avoid any loops
        self.amURNs = set() # AMs mentioned in the RSpec

    @property
    def nodes(self):
        return self._nodes

    @nodes.setter
    def nodes(self, nodeList):
        self._setListProp('nodes', nodeList, Node)

    @property
    def links(self):
        # Gets main body link elements
        return self._links

    @links.setter
    def links(self, linkList):
        self._setListProp('links', linkList, Link)

    def find_path(self, link_id):
        """Find the stitching path with the given id and return it. If no path
        matches the given id, return None.
        """
        return self.stitching and self.stitching.find_path(link_id)

    def find_link(self, hop_urn):
        """Find the main body link with the given id and return it. If no link
        matches the given id, return None.
        """
        for link in self._links:
            if link.id == link_id:
                return link
        return None


class Node(GENIObject):
    CLIENT_ID_TAG = 'client_id'
    COMPONENT_MANAGER_ID_TAG = 'component_manager_id'

    @classmethod
    def fromDOM(cls, element):
        """Parse a Node from a DOM element."""
        # FIXME: getAttributeNS?
        client_id = element.getAttribute(cls.CLIENT_ID_TAG)
        amID = element.getAttribute(cls.COMPONENT_MANAGER_ID_TAG)
        return Node(client_id, amID)

    def __init__(self, client_id, amID):
        super(Node, self).__init__()
        self.id = client_id
        self.amURN = amID

class Link(GENIObject):
    # A link from the main body of the rspec
    # Note the link client_id matches the hop_urn from the workflow matches the HopLink ID

    __ID__ = validateTextLike
    __simpleProps__ = [ ['client_id', str]]

    # XML tag constants
    CLIENT_ID_TAG = 'client_id'
    COMPONENT_MANAGER_TAG = 'component_manager'
    INTERFACE_REF_TAG = 'interface_ref'
    NAME_TAG = 'name'
    SHARED_VLAN_TAG = 'link_shared_vlan'

    @classmethod
    def fromDOM(cls, element):
        """Parse a Link from a DOM element."""
        # FIXME: getAttributeNS?
        client_id = element.getAttribute(cls.CLIENT_ID_TAG)
        refs = []
        aggs = []
        hasSharedVlan = False
        for child in element.childNodes:
            if child.localName == cls.COMPONENT_MANAGER_TAG:
                name = child.getAttribute(cls.NAME_TAG)
                agg = Aggregate.find(name)
                aggs.append(agg)
            elif child.localName == cls.INTERFACE_REF_TAG:
                # FIXME: getAttributeNS?
                c_id = child.getAttribute(cls.CLIENT_ID_TAG)
                ir = InterfaceRef(c_id)
                refs.append(ir)
            # If the link has the shared_vlan extension, note this - not a stitching reason
            elif child.localName == cls.SHARED_VLAN_TAG:
#                print 'got shared vlan'
                hasSharedVlan = True
        link = Link(client_id)
        link.aggregates = aggs
        link.interfaces = refs
        link.hasSharedVlan = hasSharedVlan
        return link

    def __init__(self, client_id):
        super(Link, self).__init__()
        self.id = client_id
        self._aggregates = []
        self._interfaces = []
        self.hasSharedVlan = False

    @property
    def interfaces(self):
        return self._interfaces

    @interfaces.setter
    def interfaces(self, interfaceList):
        self._setListProp('interfaces', interfaceList, InterfaceRef)

    @property
    def aggregates(self):
        return self._aggregates

    @aggregates.setter
    def aggregates(self, aggregateList):
        self._setListProp('aggregates', aggregateList, Aggregate)


class InterfaceRef(object):
     def __init__(self, client_id):
         self.client_id = client_id


class HopLink(object):
    # From the stitching element, the link on the hop on a path
    # Note this is Path specific

    # XML tag constants
    ID_TAG = 'id'
    HOP_TAG = 'hop'
    VLAN_TRANSLATION_TAG = 'vlanTranslation'
    VLAN_RANGE_TAG = 'vlanRangeAvailability'
    VLAN_SUGGESTED_TAG = 'suggestedVLANRange'
    SCD_TAG = 'switchingCapabilityDescriptor'
    SCSI_TAG = 'switchingCapabilitySpecificInfo'
    SCSI_L2_TAG = 'switchingCapabilitySpecificInfo_L2sc'
    CAPABILITIES_TAG = 'capabilities'
    CAPABILITY_TAG = 'capability'

    @classmethod
    def fromDOM(cls, element):
        """Parse a stitching path from a DOM element."""
        # FIXME: getAttributeNS?
        id = element.getAttribute(cls.ID_TAG)
        # FIXME: getElementsByTagNameNS?
        vlan_xlate = element.getElementsByTagName(cls.VLAN_TRANSLATION_TAG)
        if vlan_xlate:
            # If no firstChild or no nodeValue, assume false
            if len(vlan_xlate) > 0 and vlan_xlate[0].firstChild:
                x = vlan_xlate[0].firstChild.nodeValue
            else:
                x = 'False'
            vlan_translate = x.lower() in ('true')
        vlan_range = element.getElementsByTagName(cls.VLAN_RANGE_TAG)
        if vlan_range:
            # vlan_range may have no child or no nodeValue. Meaning would then be 'any'
            if len(vlan_range) > 0 and vlan_range[0].firstChild:
                vlan_range_value = vlan_range[0].firstChild.nodeValue
            else:
                vlan_range_value = "any"
            vlan_range_obj = VLANRange.fromString(vlan_range_value)
        else:
            vlan_range_obj = VLANRange()            
        vlan_suggested = element.getElementsByTagName(cls.VLAN_SUGGESTED_TAG)
        if vlan_suggested:
            # vlan_suggested may have no child or no nodeValue. Meaning would then be 'any'
            if len(vlan_suggested) > 0 and vlan_suggested[0].firstChild:
                vlan_suggested_value = vlan_suggested[0].firstChild.nodeValue
            else:
                vlan_suggested_value = "any"                
            vlan_suggested_obj = VLANRange.fromString(vlan_suggested_value)
        else:
            vlan_suggested_obj = VLANRange()            
        hoplink = HopLink(id)
        hoplink.vlan_xlate = vlan_translate
        hoplink.vlan_range_request = vlan_range_obj
        hoplink.vlan_suggested_request = vlan_suggested_obj

        # Extract the advertised capabilities
        capabilities = element.getElementsByTagName(cls.CAPABILITIES_TAG)
        if capabilities and len(capabilities) > 0 and capabilities[0].childNodes:
            hoplink.vlan_producer = False
            hoplink.vlan_consumer = False
            capabilityNodes = capabilities[0].getElementsByTagName(cls.CAPABILITY_TAG)
            if capabilityNodes and len(capabilityNodes) > 0:
                for capability in capabilityNodes:
                    if capability.firstChild:
                        cap = str(capability.firstChild.nodeValue).strip().lower()
                        hoplink.capabilities.append(cap)
                        if cap == 'producer':
                            hoplink.vlan_producer = True
                        elif cap == 'consumer':
                            hoplink.vlan_consumer = True
        return hoplink

    def __init__(self, urn):
        self.urn = urn
        self.vlan_xlate = False

        self.vlan_range_request = ""
        self.vlan_suggested_request = None
        self.vlan_range_manifest = ""
        self.vlan_suggested_manifest = None

        # If nothing advertised, assume AM only accepts tags
        self.vlan_producer = False
        self.vlan_consumer = True
        self.capabilities = [] # list of string capabilities

        self.logger = logging.getLogger('stitch.HopLink')

    def editChangesIntoDom(self, domNode, request=True):
        '''Edit any changes made in this element into the given DomNode'''
        # Note that the parent RSpec object's dom is not touched, unless this domNode is from that
        # Here we edit in the new vlan_range and vlan_available
        # If request is False, use the manifest values. Otherwise, use requested.

        # Incoming node should be the node for this hop
        nodeId = domNode.getAttribute(self.ID_TAG)
        if nodeId != self.urn:
            raise StitchingError("Hop Link %s given Dom node with different Id: %s" % (self, nodeId))

        if request:
            newVlanRangeString = str(self.vlan_range_request)
            newVlanSuggestedString = str(self.vlan_suggested_request)
        else:
            newVlanRangeString = str(self.vlan_range_manifest)
            newVlanSuggestedString = str(self.vlan_suggested_manifest)

        vlan_range = domNode.getElementsByTagName(self.VLAN_RANGE_TAG)
        if vlan_range and len(vlan_range) > 0:
            # vlan_range may have no child or no nodeValue. Meaning would then be 'any'
            if vlan_range[0].firstChild:
                # Set the value
                vlan_range[0].firstChild.nodeValue = newVlanRangeString
#                self.logger.debug("Set vlan range on node %r: %s", vlan_range[0], vlan_range[0].firstChild.nodeValue)
            else:
                vlan_range[0].appendChild(Document.createTextNode(newVlanRangeString))
        else:
            vlanRangeNode = Document.createElement(self.VLAN_RANGE_TAG)
            vlanRangeNode.appendChild(Document.createTextNode(newVlanRangeString))
            # Find the switchingCapabilitySpecificInfo_L2sc node and append it there
            l2scNodes = domNode.getElementsByTagName('switchingCapabilitySpecificInfo_L2sc')
            if l2scNodes and len(l2scNodes) > 0:
                l2scNodes[0].appendChild(vlanRangeNode)

        vlan_suggested = domNode.getElementsByTagName(self.VLAN_SUGGESTED_TAG)
        if vlan_suggested and len(vlan_suggested) > 0:
            # vlan_suggested may have no child or no nodeValue. Meaning would then be 'any'
            if vlan_suggested[0].firstChild:
                # Set the value
                vlan_suggested[0].firstChild.nodeValue = newVlanSuggestedString
#                self.logger.debug("Set vlan suggested on node %r: %s", vlan_suggested[0], vlan_suggested[0].firstChild.nodeValue)
            else:
                vlan_suggested[0].appendChild(Document.createTextNode(newVlanSuggestedString))
        else:
            vlanSuggestedNode = Document.createElement(self.VLAN_RANGE_TAG)
            vlanSuggestedNode.appendChild(Document.createTextNode(newVlanSuggestedString))
            # Find the switchingCapabilitySpecificInfo_L2sc node and append it there
            l2scNodes = domNode.getElementsByTagName('switchingCapabilitySpecificInfo_L2sc')
            if l2scNodes and len(l2scNodes) > 0:
                l2scNodes[0].appendChild(vlanSuggestedNode)