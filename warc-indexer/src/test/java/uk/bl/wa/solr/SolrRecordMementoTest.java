/*
 * Added for Simple Webcrawl Manager (SWM), 2026-09-23. Part of the SWM fork
 * of netarchivesuite/warc-indexer 3.5.1; see README-SWM.md.
 */
package uk.bl.wa.solr;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import uk.bl.wa.Memento;

/**
 * The JSON Lines output names the WARC file's path, as the XML and Solr
 * outputs do, so a document can be traced back to its record.
 */
public class SolrRecordMementoTest {

    @Test
    public void theJsonDocumentCarriesTheSourceFilePath() throws Exception {
        SolrRecord solr = SolrRecordFactory.createFactory(null).createRecord();
        solr.addField(SolrFields.ID, "20260901221521/abc");
        solr.addField(SolrFields.SOLR_URL, "https://example.org/");
        solr.addField(SolrFields.SOURCE_FILE, "sample.warc.gz");
        solr.addField(SolrFields.SOURCE_FILE_PATH, "/data/warcs/116/sample.warc.gz");
        solr.addField(SolrFields.SOURCE_FILE_OFFSET, "4903324");

        Memento memento = solr.toMemento();
        assertEquals("/data/warcs/116/sample.warc.gz", memento.getSourceFilePath());

        JsonNode json = new ObjectMapper().readTree(memento.toJSON());
        assertEquals("/data/warcs/116/sample.warc.gz", json.get("source_file_path").asText());
        assertEquals("sample.warc.gz", json.get("source_file").asText());
        assertEquals(4903324L, json.get("source_file_offset").asLong());
        assertTrue(json.has("url"));
    }
}
