/*
 * (C) Copyright 2022 NOAA/NWS/NCEP/EMC
 *
 * This software is licensed under the terms of the Apache Licence Version 2.0
 * which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
 */

#include "ResultSet.h"

#include <algorithm>
#include <string>

#include "VectorMath.h"


namespace Ingester {
namespace bufr {
    ResultSet::ResultSet(const std::vector<std::string>& names) :
      names_(names)
    {
        fieldWidths.resize(names.size());
    }

    ResultSet::~ResultSet()
    {
    }

    std::shared_ptr<Ingester::DataObjectBase>
        ResultSet::get(const std::string& fieldName, const std::string& groupByFieldName) const
    {
        std::vector<double> data;
        std::vector<int> dims;
        std::vector<std::string> dimPaths;

        getRawValues(fieldName,
                     groupByFieldName,
                     data,
                     dims,
                     dimPaths);

        std::shared_ptr<Ingester::DataObjectBase> object;
        if (unit(fieldName) == "CCITT IA5")
        {
            object =  std::make_shared<Ingester::DataObject<std::string>>();
        }
        else if (unit(fieldName) == "CODE TABLE" ||
                 unit(fieldName) == "FLAG TABLE" ||
                 unit(fieldName) == "NUMERIC")
        {
            object =  std::make_shared<Ingester::DataObject<uint32_t>>();
        }
        else
        {
            object = std::make_shared<Ingester::DataObject<float>>();
        }

        object->setData(data, MissingValue);
        object->setDims(dims);
        object->setFieldName(fieldName);
        object->setGroupByFieldName(groupByFieldName);

        // Add dim path strings
        const char* ws = " \t\n\r\f\v";
        std::vector<std::string> paths(dims.size());
        for (size_t dimIdx = 0; dimIdx < dims.size(); dimIdx++)
        {
            auto path_str = dimPaths[dimIdx];

            // Trim extra chars from the path str
            path_str.erase(path_str.find_last_not_of(ws) + 1);
            paths[dimIdx] = path_str;
        }

        object->setDimPaths(paths);

        return object;
    }


    DataFrame& ResultSet::nextDataFrame()
    {
        dataFrames_.push_back(DataFrame(names_.size()));
        return dataFrames_.back();
    }

    void ResultSet::getRawValues(const std::string& fieldName,
                                 const std::string& groupByField,
                                 std::vector<double>& data,
                                 std::vector<int>& dims,
                                 std::vector<std::string>& dimPaths) const
    {
        // Find the dims based on the largest sequence counts in the fields

        // Compute Dims
        std::vector<int> dimsList;
        std::vector<int> exportDims;
        int groupbyIdx = 0;
        int totalGroupbyElements = 0;

        int targetFieldIdx = 0;
        int groupByFieldIdx = 0;
        if (dataFrames_.size() > 0)
        {
            targetFieldIdx = dataFrames_[0].fieldIndexForNodeNamed(fieldName);

            if (groupByField != "")
            {
                groupByFieldIdx = dataFrames_[0].fieldIndexForNodeNamed(groupByField);
            }

            auto& targetField = dataFrames_[0].fieldAtIdx(targetFieldIdx);
            dimPaths = targetField.dimPaths;

            exportDims = targetField.exportDims;
        }

        for (auto& dataFrame : dataFrames_)
        {
            auto& targetField = dataFrame.fieldAtIdx(targetFieldIdx);
            if (!targetField.dimPaths.empty() && dimPaths.size() < targetField.dimPaths.size())
            {
                dimPaths = targetField.dimPaths;
                exportDims = targetField.exportDims;
            }

            size_t dimsLen = targetField.seqCounts.size();
            if (dimsList.size() < dimsLen)
            {
                dimsList.resize(dimsLen, 0);
            }

            for (size_t cntIdx = 0; cntIdx < targetField.seqCounts.size(); ++cntIdx)
            {
                if (!targetField.seqCounts[cntIdx].empty())
                {
                    dimsList[cntIdx] = std::max(dimsList[cntIdx],
                                                max(targetField.seqCounts[cntIdx]));
                }
            }

            if (groupByField != "")
            {
                auto& groupByField = dataFrame.fieldAtIdx(groupByFieldIdx);
                groupbyIdx = std::max(groupbyIdx, static_cast<int>(groupByField.seqCounts.size()));

                if (groupbyIdx > static_cast<int>(dimsList.size()))
                {
                    dimPaths = {groupByField.dimPaths.back()};

                    int groupbyElementsForFrame = 1;
                    for (auto &seqCount : groupByField.seqCounts)
                    {
                        if (!seqCount.empty())
                        {
                            groupbyElementsForFrame *= max(seqCount);
                        }
                    }

                    totalGroupbyElements = std::max(totalGroupbyElements, groupbyElementsForFrame);
                }
                else
                {
                    dimPaths = {};
                    for (size_t targetIdx = groupByField.exportDims.size() - 1;
                         targetIdx < targetField.dimPaths.size();
                         ++targetIdx)
                    {
                        dimPaths.push_back(targetField.dimPaths[targetIdx]);
                    }
                }
            }
        }

        auto allDims = dimsList;

        // If there is absolutely no data for a field you will have the problem were the
        // size of some dimensions are zero. We need to have at least 1 element in each
        // dimension to make room for the missing value. This if statement makes sure there
        // is at least 1 element in each dimension.
        for (size_t dimIdx = 0; dimIdx < allDims.size(); ++dimIdx)
        {
            if (allDims[dimIdx] == 0)
            {
                allDims[dimIdx] = 1;
            }
        }

        if (groupbyIdx > 0)
        {
            // The groupby field occurs at the same or greater repetition level as the target field.
            if (groupbyIdx > static_cast<int>(dimsList.size()))
            {
                dims.resize(1, totalGroupbyElements);
                exportDims = {0};
                allDims = dims;
            }
            // The groupby field occurs at a lower repetition level than the target field.
            else
            {
                dims.resize(dimsList.size() - groupbyIdx + 1, 1);
                for (auto dimIdx = 0; dimIdx < groupbyIdx; ++dimIdx)
                {
                    dims[0] *= allDims[dimIdx];
                }

                for (size_t dimIdx = groupbyIdx; dimIdx < allDims.size(); ++dimIdx)
                {
                    dims[dimIdx - groupbyIdx + 1] = allDims[dimIdx];
                }

                exportDims = exportDims - (groupbyIdx - 1);

                // Filter out exportDims that are 0
                std::vector<int> filteredExportDims;
                for (size_t dimIdx = 0; dimIdx < exportDims.size(); ++dimIdx)
                {
                    if (exportDims[dimIdx] >= 0)
                    {
                        filteredExportDims.push_back(exportDims[dimIdx]);
                    }
                }

                if (filteredExportDims.empty() || filteredExportDims[0] != 0)
                {
                    filteredExportDims.insert(filteredExportDims.begin(), 0);
                }

                exportDims = filteredExportDims;
            }
        }
        else
        {
            dims = allDims;
        }

        size_t totalRows = dims[0] * dataFrames_.size();

        // Make data set
        int rowLength = 1;
        for (size_t dimIdx = 1; dimIdx < dims.size(); ++dimIdx)
        {
            rowLength *= dims[dimIdx];
        }

        data.resize(totalRows * rowLength, MissingValue);
        for (size_t frameIdx = 0; frameIdx < dataFrames_.size(); ++frameIdx)
        {
            auto& dataFrame = dataFrames_[frameIdx];
            std::vector<std::vector<double>> frameData;
            auto& targetField = dataFrame.fieldAtIdx(targetFieldIdx);

            if (!targetField.missing) {
                getRowsForField(targetField,
                                frameData,
                                allDims,
                                groupbyIdx);

                auto dataRowIdx = dims[0] * frameIdx;
                for (size_t rowIdx = 0; rowIdx < frameData.size(); ++rowIdx)
                {
                    auto &row = frameData[rowIdx];
                    for (size_t colIdx = 0; colIdx < row.size(); ++colIdx)
                    {
                        data[dataRowIdx*rowLength + rowIdx * row.size() + colIdx] = row[colIdx];
                    }
                }
            }
        }

        // Convert dims per data frame to dims for all the collected data.
        dims[0] = totalRows;
        if (dataFrames_.size() > 1)
        {
            dims = slice(dims, exportDims);
        }
    }

//    subroutine result_set__get_rows_for_field(self, target_field, data_rows, dims, groupby_idx)

    void ResultSet::getRowsForField(const DataField& targetField,
                                    std::vector<std::vector<double>>& dataRows,
                                    const std::vector<int>& dims,
                                    int groupbyIdx) const
    {
        size_t maxCounts = 0;
        std::vector<size_t> idxs(targetField.data.size());
        for (size_t i = 0; i < idxs.size(); ++i)
        {
            idxs[i] = i;
        }

        // Compute max counts
        for (size_t i = 0; i < targetField.seqCounts.size(); ++i)
        {
            if (maxCounts < targetField.seqCounts[i].size())
            {
                maxCounts = targetField.seqCounts[i].size();
            }
        }

        // Compute insert array
        std::vector<std::vector<int>> inserts(dims.size(), {0});
        for (size_t repIdx = 0;
             repIdx < std::min(dims.size(), targetField.seqCounts.size());
             ++repIdx)
        {
            inserts[repIdx] = product<int>(dims.begin() + repIdx, dims.end()) - \
                              targetField.seqCounts[repIdx] * \
                              product<int>(dims.begin() + repIdx + 1, dims.end());
        }

        // Inflate the data, compute the idxs for each data element in the result array
        for (int dim_idx = dims.size() - 1; dim_idx >= 0; --dim_idx)
        {
            for (size_t insert_idx = 0; insert_idx < inserts[dim_idx].size(); ++insert_idx)
            {
                size_t num_inserts = inserts[dim_idx][insert_idx];
                if (num_inserts > 0)
                {
                    int data_idx = product<int>(dims.begin() + dim_idx, dims.end()) *
                            insert_idx + product<int>(dims.begin() + dim_idx, dims.end())
                                    - num_inserts - 1;

                    for (size_t i = 0; i < idxs.size(); ++i)
                    {
                        if (static_cast<int>(idxs[i]) > data_idx)
                        {
                            idxs[i] += num_inserts;
                        }
                    }
                }
            }
        }

        auto output = std::vector<double>(product(dims), 10.0e10);
        for (size_t i = 0; i < idxs.size(); ++i)
        {
            output[idxs[i]] = targetField.data[i];
        }

        // Apply groupBy and make output
        if (groupbyIdx > 0)
        {
            if (groupbyIdx > static_cast<int>(targetField.seqCounts.size()))
            {
                size_t numRows = product(dims);
                dataRows.resize(numRows * maxCounts, {MissingValue});
                for (size_t i = 0; i < numRows; ++i)
                {
                    if (output.size())
                    {
                        dataRows[i][0] = output[0];
                    }
                }
            }
            else
            {
                size_t numRows = product<int>(dims.begin(), dims.begin() + groupbyIdx);
                std::vector<int> rowDims;
                rowDims.assign(dims.begin() + groupbyIdx, dims.end());

                size_t numsPerRow = static_cast<size_t>(product(rowDims));
                dataRows.resize(numRows, std::vector<double>(numsPerRow, MissingValue));
                for (size_t i = 0; i < numRows; ++i)
                {
                    for (size_t j = 0; j < numsPerRow; ++j)
                    {
                        dataRows[i][j] = output[i * numsPerRow + j];
                    }
                }
            }
        }
        else
        {
            dataRows.resize(1);
            dataRows[0] = output;
        }
    }

    std::string ResultSet::unit(const std::string& fieldName) const
    {
        auto fieldIdx = dataFrames_.front().fieldIndexForNodeNamed(fieldName);
        return dataFrames_.front().fieldAtIdx(fieldIdx).unit;
    }
}  // namespace bufr
}  // namespace Ingester
