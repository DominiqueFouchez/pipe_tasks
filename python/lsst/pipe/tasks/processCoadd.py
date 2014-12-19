#
# LSST Data Management System
# Copyright 2008-2015 AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.afw.geom as afwGeom
import lsst.afw.table as afwTable
from lsst.coadd.utils import CoaddDataIdContainer
from .coaddBase import getSkyInfo, scaleVariance
from .processImage import ProcessImageTask
from lsst.meas.astrom import AstrometryTask
from .propagateVisitFlags import PropagateVisitFlagsTask

class ProcessCoaddConfig(ProcessImageTask.ConfigClass):
    """Config for ProcessCoadd"""
    coaddName = pexConfig.Field(
        dtype = str,
        default = "deep",
        doc = "coadd name: typically one of deep or goodSeeing",
    )
    doScaleVariance = pexConfig.Field(
        dtype = bool,
        default = True,
        doc = "Scale variance plane using empirical noise",
    )
    mask = pexConfig.ListField(
        dtype=str,
        default=["DETECTED", "BAD", "SAT", "NO_DATA", "INTRP"],
        doc = "Mask planes for pixels to ignore when scaling variance"
    )
    astrometry = pexConfig.ConfigurableField(
        target = AstrometryTask,
        doc = "Astrometric matching, for matching sources to reference",
    )
    propagateFlags = pexConfig.ConfigurableField(target=PropagateVisitFlagsTask,
                                                 doc="Propagate flags to coadd")

    def setDefaults(self):
        ProcessImageTask.ConfigClass.setDefaults(self)
        self.detection.background.undersampleStyle = 'REDUCE_INTERP_ORDER'
        self.detection.thresholdType = "pixel_stdev"
        self.detection.isotropicGrow = True
        self.detection.returnOriginalFootprints = False
        self.doWriteSourceMatches = True
        self.measurement.doReplaceWithNoise = True
        # coadds do not yet have ap corr data; once they do, delete the following to enable the
        # ProcessImageConfig default of yes; meanwhile the warning may help remind us
        self.measurement.doApplyApCorr = "noButWarn"
        self.doDeblend = True
        self.deblend.maxNumberOfPeaks = 20
        self.astrometry.forceKnownWcs = True

        self.measurement.plugins.names |= ['base_InputCount']
        # The following line must be set if clipped pixel flags are to be added to the output table
        # The clipped mask plane is added by running SafeClipAssembleCoaddTask
        self.measurement.plugins['base_PixelFlags'].masksFpAnywhere = ['CLIPPED']

class ProcessCoaddTask(ProcessImageTask):
    """Process a Coadd image
    """
    ConfigClass = ProcessCoaddConfig
    _DefaultName = "processCoadd"

    def __init__(self, **kwargs):
        ProcessImageTask.__init__(self, **kwargs)
        self.dataPrefix = self.config.coaddName + "Coadd_"
        self.isPatchInnerKey = self.schema.addField(
            "detect_isPatchInner", type="Flag",
            doc="true if source is in the inner region of a coadd patch",
        )
        self.isTractInnerKey = self.schema.addField(
            "detect_isTractInner", type="Flag",
            doc="true if source is in the inner region of a coadd tract",
        )
        self.isPrimaryKey = self.schema.addField(
            "detect_isPrimary", type="Flag",
            doc="true if source has no children and is in the inner region of a coadd patch " \
                + "and is in the inner region of a coadd tract",
        )
        self.makeSubtask("propagateFlags", schema=self.schema)
        if self.config.doWriteSourceMatches:
            self.makeSubtask("astrometry", schema=self.schema)

    def makeIdFactory(self, dataRef):
        expBits = dataRef.get(self.config.coaddName + "CoaddId_bits")
        expId = long(dataRef.get(self.config.coaddName + "CoaddId"))
        return afwTable.IdFactory.makeSource(expId, 64 - expBits)

    def getExposureId(self, dataRef):
        return long(dataRef.get(self.config.coaddName + "CoaddId"))

    def getAstrometer(self):
        return self.astrometry

    @pipeBase.timeMethod
    def run(self, dataRef):
        """Process a coadd image

        @param dataRef: butler data reference corresponding to coadd patch
        @return pipe_base Struct containing these fields:
        - exposure: input exposure, as modified in the course of processing
        - sources: detected source if config.doDetection, else None
        """
        self.log.info("Processing %s" % (dataRef.dataId))

        # initialize outputs
        skyInfo = getSkyInfo(coaddName=self.config.coaddName, patchRef=dataRef)

        coadd = dataRef.get(self.config.coaddName + "Coadd")
        if self.config.doScaleVariance:
            scaleVariance(coadd.getMaskedImage(), self.config.mask, log=self.log)

        # delegate most of the work to ProcessImageTask
        result = self.process(dataRef, coadd, enableWriteSources=False)
        result.coadd = coadd

        if result.sources is not None:
            self.setIsPrimaryFlag(sources=result.sources, skyInfo=skyInfo)
            self.propagateFlags.run(dataRef.getButler(), result.sources,
                                    self.propagateFlags.getCcdInputs(coadd), coadd.getWcs())

            # write sources
            if self.config.doWriteSources:
                dataRef.put(result.sources, self.dataPrefix + 'src')

        return result

    def setIsPrimaryFlag(self, sources, skyInfo):
        """Set is-primary and related flags on sources

        @param[in,out] sources: a SourceTable
            - reads centroid fields and an nChild field
            - writes is-patch-inner, is-tract-inner and is-primary flags
        @param[in] skyInfo: a SkyInfo object as returned by getSkyInfo;
            reads skyMap, patchInfo, and tractInfo fields

        @raise RuntimeError if self.config.doDeblend and the nChild key is not found in the table
        """
        # Test for the presence of the nchild key instead of checking config.doDeblend because sources
        # might be unpersisted with deblend info, even if deblending is not run again.
        nChildKeyName = "deblend_nChild"

        try:
            nChildKey = self.schema.find(nChildKeyName).key
        except Exception:
            nChildKey = None

        if self.config.doDeblend and nChildKey is None:
            # deblending was run but the nChildKey was not found; this suggests a variant deblender
            # was used that we cannot use the output from, or some obscure error.
            raise RuntimeError("Ran the deblender but cannot find %r in the source table" % (nChildKeyName,))

        # set inner flags for each source and set primary flags for sources with no children
        # (or all sources if deblend info not available)
        innerFloatBBox = afwGeom.Box2D(skyInfo.patchInfo.getInnerBBox())
        tractId = skyInfo.tractInfo.getId()
        for source in sources:
            if source.getCentroidFlag():
                # centroid unknown, so leave the inner and primary flags False
                continue

            centroidPos = source.getCentroid()
            isPatchInner = innerFloatBBox.contains(centroidPos)
            source.setFlag(self.isPatchInnerKey, isPatchInner)

            skyPos = source.getCoord()
            sourceInnerTractId = skyInfo.skyMap.findTract(skyPos).getId()
            isTractInner = sourceInnerTractId == tractId
            source.setFlag(self.isTractInnerKey, isTractInner)

            if nChildKey is None or source.get(nChildKey) == 0:
                source.setFlag(self.isPrimaryKey, isPatchInner and isTractInner)

    @classmethod
    def _makeArgumentParser(cls):
        parser = pipeBase.ArgumentParser(name=cls._DefaultName)
        parser.add_id_argument("--id", "deepCoadd", help="data ID, e.g. --id tract=12345 patch=1,2",
                               ContainerClass=CoaddDataIdContainer)
        return parser

    def _getConfigName(self):
        """Return the name of the config dataset
        """
        return "%s_processCoadd_config" % (self.config.coaddName,)

    def _getMetadataName(self):
        """Return the name of the metadata dataset
        """
        return "%s_processCoadd_metadata" % (self.config.coaddName,)
