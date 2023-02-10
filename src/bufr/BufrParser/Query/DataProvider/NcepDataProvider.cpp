/*
 * (C) Copyright 2022 NOAA/NWS/NCEP/EMC
 *
 * This software is licensed under the terms of the Apache Licence Version 2.0
 * which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
 */

#include "NcepDataProvider.h"
#include "bufr_interface.h"

#include <algorithm>
#include <iostream>
#include <sstream>

#include "eckit/exception/Exceptions.h"


namespace Ingester {
namespace bufr {
    NcepDataProvider::NcepDataProvider(const std::string& filePath) :
      DataProvider(filePath)
    {
    }

    void NcepDataProvider::open()
    {
        open_f(FileUnit, filePath_.c_str());
        openbf_f(FileUnit, "IN", FileUnit);

        isOpen_ = true;
    }

    void NcepDataProvider::updateTable(const std::string& subset)
    {
        int size = 0;
        int *intPtr = nullptr;
        int strLen = 0;
        char *charPtr = nullptr;

        if (currentTableData_ = tableCache_[subset])
        {
            return;
        }

        if (currentTableData_ == nullptr ||      // If the table is not in the cache
            getInode() > numTags() ||            // or there are not enough tags
            getTag(getInode()) != subset)   // or the tag is inconsistent with the subset we expect
        {
            deleteData();  // in case we previously loaded data

            currentTableData_ = std::make_shared<TableData>();

            get_isc_f(&intPtr, &size);
            currentTableData_->isc = std::vector<int>(intPtr, intPtr + size);

            get_link_f(&intPtr, &size);
            currentTableData_->link = std::vector<int>(intPtr, intPtr + size);

            get_itp_f(&intPtr, &size);
            currentTableData_->itp = std::vector<int>(intPtr, intPtr + size);

            get_typ_f(&charPtr, &strLen, &size);
            currentTableData_->typ.resize(size);
            for (int wordIdx = 0; wordIdx < size; wordIdx++) {
                auto typ = std::string(&charPtr[wordIdx * strLen], strLen);
                currentTableData_->typ[wordIdx] = TypMap.at(typ);
            }

            get_tag_f(&charPtr, &strLen, &size);
            currentTableData_->tag.resize(size);
            for (int wordIdx = 0; wordIdx < size; wordIdx++) {
                auto tag = std::string(&charPtr[wordIdx * strLen], strLen);
                currentTableData_->tag[wordIdx] = tag.substr(0, tag.find_first_of(' '));
            }

            get_jmpb_f(&intPtr, &size);
            currentTableData_->jmpb = std::vector<int>(intPtr, intPtr + size);
        }

        tableCache_[subset] = currentTableData_;
    }

    size_t NcepDataProvider::variantId() const
    {
        return 0;
    }

    bool NcepDataProvider::hasVariants() const
    {
        return false;
    }
}  // namespace bufr
}  // namespace Ingester
